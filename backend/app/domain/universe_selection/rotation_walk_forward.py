from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import asdict, dataclass, replace
from datetime import date
from statistics import mean, stdev
from typing import Mapping, Sequence

from app.core.market_calendar import get_session
from app.domain.universe_selection.catalog import IndexCandidate
from app.domain.universe_selection.selector import (
    CandidateInput,
    DailyBar,
    UniverseSelectionConfig,
    liquidity_spread_proxy_bps,
    select_candidates,
)


ROTATION_WALK_FORWARD_VERSION = "rotation-monthly-open-walk-forward-v1"
ROTATION_BENCHMARK_SYMBOLS = ("QQQ.US", "DIA.US")
_CASH = "__CASH__"


@dataclass(frozen=True)
class RotationVariant:
    name: str
    lookback_bars: int
    skip_bars: int
    sma_bars: int
    max_selected: int
    max_per_risk_group: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("rotation variant name must not be empty")
        if self.lookback_bars <= self.skip_bars:
            raise ValueError("lookback_bars must exceed skip_bars")
        if self.skip_bars < 1 or self.sma_bars < 2:
            raise ValueError("skip_bars and sma_bars are invalid")
        if self.max_selected < 1 or self.max_per_risk_group < 1:
            raise ValueError("selection limits must be positive")


DEFAULT_ROTATION_VARIANTS: tuple[RotationVariant, ...] = (
    RotationVariant(
        name="incumbent_top10_12_1",
        lookback_bars=252,
        skip_bars=21,
        sma_bars=200,
        max_selected=10,
        max_per_risk_group=2,
    ),
    RotationVariant(
        name="concentrated_top8_12_1",
        lookback_bars=252,
        skip_bars=21,
        sma_bars=200,
        max_selected=8,
        max_per_risk_group=2,
    ),
    RotationVariant(
        name="concentrated_top6_12_1",
        lookback_bars=252,
        skip_bars=21,
        sma_bars=200,
        max_selected=6,
        max_per_risk_group=2,
    ),
    RotationVariant(
        name="faster_top8_6_1",
        lookback_bars=126,
        skip_bars=21,
        sma_bars=126,
        max_selected=8,
        max_per_risk_group=2,
    ),
    RotationVariant(
        name="diversified_top8_12_1",
        lookback_bars=252,
        skip_bars=21,
        sma_bars=200,
        max_selected=8,
        max_per_risk_group=1,
    ),
)


@dataclass(frozen=True)
class RotationPeriod:
    signal_date: date
    entry_date: date
    exit_date: date
    selected_symbols: tuple[str, ...]
    gross_return_pct: float
    net_return_pct: float
    transaction_cost_pct: float
    turnover_pct: float
    qqq_return_pct: float
    dia_return_pct: float


@dataclass(frozen=True)
class RotationPerformance:
    periods: int = 0
    total_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    annualized_volatility_pct: float = 0.0
    sharpe: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate_pct: float = 0.0
    average_turnover_pct: float = 0.0
    total_cost_pct: float = 0.0
    average_holdings: float = 0.0
    qqq_total_return_pct: float = 0.0
    qqq_annualized_return_pct: float = 0.0
    qqq_sharpe: float = 0.0
    qqq_max_drawdown_pct: float = 0.0
    dia_total_return_pct: float = 0.0
    dia_annualized_return_pct: float = 0.0
    dia_sharpe: float = 0.0
    dia_max_drawdown_pct: float = 0.0
    excess_annualized_return_vs_qqq_pct: float = 0.0
    excess_annualized_return_vs_dia_pct: float = 0.0


@dataclass(frozen=True)
class RotationVariantEvaluation:
    variant: RotationVariant
    training_score: float
    validation_passed: bool
    validation_blockers: tuple[str, ...]
    full: RotationPerformance
    training: RotationPerformance
    validation: RotationPerformance


@dataclass(frozen=True)
class RotationWalkForwardResult:
    algorithm_version: str
    status: str
    benchmark_symbols: tuple[str, ...]
    data_scope: str
    survivorship_bias: bool
    validation_periods: int
    selected_variant: str | None
    selected_variant_validation_passed: bool
    validated_challenger_variant: str | None
    automatic_promotion_allowed: bool
    promotion_blockers: tuple[str, ...]
    variants: tuple[RotationVariantEvaluation, ...]
    selected_variant_periods: tuple[RotationPeriod, ...]
    validated_challenger_periods: tuple[RotationPeriod, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["selected_variant_periods"] = [
            {
                **asdict(period),
                "signal_date": period.signal_date.isoformat(),
                "entry_date": period.entry_date.isoformat(),
                "exit_date": period.exit_date.isoformat(),
            }
            for period in self.selected_variant_periods
        ]
        payload["validated_challenger_periods"] = [
            {
                **asdict(period),
                "signal_date": period.signal_date.isoformat(),
                "entry_date": period.entry_date.isoformat(),
                "exit_date": period.exit_date.isoformat(),
            }
            for period in self.validated_challenger_periods
        ]
        return payload


def _bar_date(bar: DailyBar) -> date:
    return get_session("US").local(bar.timestamp).date()


def _bar_map(bars: Sequence[DailyBar]) -> dict[date, DailyBar]:
    result: dict[date, DailyBar] = {}
    for bar in sorted(bars, key=lambda item: item.timestamp):
        session_date = _bar_date(bar)
        values = (
            float(bar.open),
            float(bar.high),
            float(bar.low),
            float(bar.close),
        )
        if (
            all(math.isfinite(value) and value > 0 for value in values)
            and float(bar.high) >= max(values[0], values[2], values[3])
            and float(bar.low) <= min(values[0], values[1], values[3])
        ):
            result[session_date] = bar
    return result


def _monthly_rebalance_dates(
    benchmark_maps: Mapping[str, Mapping[date, DailyBar]],
) -> tuple[list[date], list[date]]:
    if any(symbol not in benchmark_maps for symbol in ROTATION_BENCHMARK_SYMBOLS):
        return [], []
    common_dates = sorted(
        set(benchmark_maps["QQQ.US"]) & set(benchmark_maps["DIA.US"])
    )
    rebalance_dates: list[date] = []
    prior_month: tuple[int, int] | None = None
    for session_date in common_dates:
        month = (session_date.year, session_date.month)
        if month != prior_month:
            rebalance_dates.append(session_date)
            prior_month = month
    return common_dates, rebalance_dates


def _candidate_inputs_at(
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
        dates, ordered_bars = histories_by_symbol.get(
            candidate.symbol,
            ((), ()),
        )
        end = bisect_right(dates, signal_date)
        start = max(0, end - max_bars)
        bars = ordered_bars[start:end]
        errors: tuple[str, ...] = ()
        if not bars or dates[end - 1] != signal_date:
            errors = ("DATA_STALE_SESSION_DATE",)
        inputs.append(
            CandidateInput(
                candidate=candidate,
                completed_daily_bars=bars,
                bid=None,
                ask=None,
                estimated_spread_bps=liquidity_spread_proxy_bps(bars),
                data_errors=errors,
            )
        )
    return inputs


def _trade_cost_and_turnover(
    old_weights: Mapping[str, float],
    new_weights: Mapping[str, float],
    *,
    round_trip_cost_bps: float,
    spread_bps_by_symbol: Mapping[str, float],
    fallback_spread_bps: float,
) -> tuple[float, float]:
    assets = (set(old_weights) | set(new_weights)) - {_CASH}
    buys = sum(
        max(new_weights.get(symbol, 0.0) - old_weights.get(symbol, 0.0), 0.0)
        for symbol in assets
    )
    sells = sum(
        max(old_weights.get(symbol, 0.0) - new_weights.get(symbol, 0.0), 0.0)
        for symbol in assets
    )
    cash_change = abs(
        new_weights.get(_CASH, 0.0) - old_weights.get(_CASH, 0.0)
    )
    turnover = 0.5 * (buys + sells + cash_change)
    cost = sum(
        abs(
            new_weights.get(symbol, 0.0)
            - old_weights.get(symbol, 0.0)
        )
        * (
            round_trip_cost_bps
            + spread_bps_by_symbol.get(
                symbol,
                fallback_spread_bps,
            )
        )
        / 2
        / 10_000
        for symbol in assets
    )
    return cost, turnover


def _target_weights(symbols: Sequence[str]) -> dict[str, float]:
    if not symbols:
        return {_CASH: 1.0}
    weight = 1.0 / len(symbols)
    return {symbol: weight for symbol in symbols} | {_CASH: 0.0}


def _simulate_variant(
    *,
    candidates: Sequence[IndexCandidate],
    histories_by_symbol: Mapping[
        str,
        tuple[tuple[date, ...], tuple[DailyBar, ...]],
    ],
    candidate_maps: Mapping[str, Mapping[date, DailyBar]],
    benchmark_maps: Mapping[str, Mapping[date, DailyBar]],
    common_dates: Sequence[date],
    rebalance_dates: Sequence[date],
    base_config: UniverseSelectionConfig,
    variant: RotationVariant,
) -> list[RotationPeriod]:
    if len(rebalance_dates) < 2:
        return []
    date_position = {
        session_date: index for index, session_date in enumerate(common_dates)
    }
    selection_config = replace(
        base_config,
        rotation_lookback_bars=variant.lookback_bars,
        rotation_skip_bars=variant.skip_bars,
        rotation_sma_bars=variant.sma_bars,
        rotation_max_selected=variant.max_selected,
        rotation_max_per_risk_group=(
            variant.max_per_risk_group
        ),
    )
    round_trip_cost_bps = (
        selection_config.round_trip_fee_bps
        + selection_config.round_trip_slippage_bps
    )
    required_bars = max(
        selection_config.min_completed_bars,
        selection_config.rotation_lookback_bars + 1,
        selection_config.rotation_sma_bars,
    )
    current_weights: dict[str, float] = {_CASH: 1.0}
    periods: list[RotationPeriod] = []
    for entry_date, exit_date in zip(
        rebalance_dates,
        rebalance_dates[1:],
    ):
        entry_position = date_position.get(entry_date)
        if entry_position is None or entry_position == 0:
            continue
        signal_date = common_dates[entry_position - 1]
        selections = select_candidates(
            _candidate_inputs_at(
                candidates=candidates,
                histories_by_symbol=histories_by_symbol,
                signal_date=signal_date,
                max_bars=required_bars,
            ),
            selection_config,
        )
        if not any(
            row.rotation.momentum_pct is not None
            for row in selections
        ):
            continue
        spread_bps_by_symbol = {
            row.candidate.symbol: (
                row.metrics.relative_spread_bps
                if row.metrics.relative_spread_bps is not None
                else selection_config.max_relative_spread_bps
            )
            for row in selections
        }
        ranked = sorted(
            (
                row
                for row in selections
                if row.rotation.selected
            ),
            key=lambda row: row.rotation.rank or 10_000,
        )
        tradable: list[str] = []
        returns: dict[str, float] = {}
        for row in ranked:
            symbol = row.candidate.symbol
            entry_bar = candidate_maps.get(symbol, {}).get(entry_date)
            exit_bar = candidate_maps.get(symbol, {}).get(exit_date)
            if entry_bar is None:
                continue
            entry_price = float(entry_bar.open)
            if entry_price <= 0:
                continue
            tradable.append(symbol)
            returns[symbol] = (
                float(exit_bar.open) / entry_price - 1.0
                if exit_bar is not None
                else -1.0
            )
        new_weights = _target_weights(tradable)
        cost, turnover = _trade_cost_and_turnover(
            current_weights,
            new_weights,
            round_trip_cost_bps=round_trip_cost_bps,
            spread_bps_by_symbol=spread_bps_by_symbol,
            fallback_spread_bps=(
                selection_config.max_relative_spread_bps
            ),
        )
        gross_return = sum(
            new_weights.get(symbol, 0.0) * asset_return
            for symbol, asset_return in returns.items()
        )
        net_return = (1.0 - cost) * (1.0 + gross_return) - 1.0
        ending_values = {
            symbol: weight * (1.0 + returns.get(symbol, 0.0))
            for symbol, weight in new_weights.items()
        }
        ending_total = sum(ending_values.values())
        current_weights = (
            {
                symbol: value / ending_total
                for symbol, value in ending_values.items()
            }
            if ending_total > 0
            else {_CASH: 1.0}
        )
        qqq_entry = benchmark_maps["QQQ.US"][entry_date]
        qqq_exit = benchmark_maps["QQQ.US"][exit_date]
        dia_entry = benchmark_maps["DIA.US"][entry_date]
        dia_exit = benchmark_maps["DIA.US"][exit_date]
        periods.append(
            RotationPeriod(
                signal_date=signal_date,
                entry_date=entry_date,
                exit_date=exit_date,
                selected_symbols=tuple(tradable),
                gross_return_pct=gross_return * 100,
                net_return_pct=net_return * 100,
                transaction_cost_pct=cost * 100,
                turnover_pct=turnover * 100,
                qqq_return_pct=(
                    float(qqq_exit.open) / float(qqq_entry.open) - 1.0
                )
                * 100,
                dia_return_pct=(
                    float(dia_exit.open) / float(dia_entry.open) - 1.0
                )
                * 100,
            )
        )
    return periods


def _compound(returns: Sequence[float]) -> float:
    equity = 1.0
    for value in returns:
        equity *= 1.0 + value
    return equity - 1.0


def _annualized_return(returns: Sequence[float]) -> float:
    if not returns:
        return 0.0
    terminal = 1.0 + _compound(returns)
    if terminal <= 0:
        return -1.0
    return terminal ** (12 / len(returns)) - 1.0


def _sharpe(returns: Sequence[float]) -> float:
    if len(returns) < 2:
        return 0.0
    dispersion = stdev(returns)
    return mean(returns) / dispersion * math.sqrt(12) if dispersion > 0 else 0.0


def _max_drawdown(returns: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, 1.0 - equity / peak)
    return worst


def _performance(periods: Sequence[RotationPeriod]) -> RotationPerformance:
    if not periods:
        return RotationPerformance()
    strategy = [period.net_return_pct / 100 for period in periods]
    qqq = [period.qqq_return_pct / 100 for period in periods]
    dia = [period.dia_return_pct / 100 for period in periods]
    strategy_annualized = _annualized_return(strategy)
    qqq_annualized = _annualized_return(qqq)
    dia_annualized = _annualized_return(dia)
    volatility = (
        stdev(strategy) * math.sqrt(12)
        if len(strategy) >= 2
        else 0.0
    )
    return RotationPerformance(
        periods=len(periods),
        total_return_pct=_compound(strategy) * 100,
        annualized_return_pct=strategy_annualized * 100,
        annualized_volatility_pct=volatility * 100,
        sharpe=_sharpe(strategy),
        max_drawdown_pct=_max_drawdown(strategy) * 100,
        win_rate_pct=sum(value > 0 for value in strategy) / len(strategy) * 100,
        average_turnover_pct=mean(
            period.turnover_pct for period in periods
        ),
        total_cost_pct=sum(
            period.transaction_cost_pct for period in periods
        ),
        average_holdings=mean(
            len(period.selected_symbols) for period in periods
        ),
        qqq_total_return_pct=_compound(qqq) * 100,
        qqq_annualized_return_pct=qqq_annualized * 100,
        qqq_sharpe=_sharpe(qqq),
        qqq_max_drawdown_pct=_max_drawdown(qqq) * 100,
        dia_total_return_pct=_compound(dia) * 100,
        dia_annualized_return_pct=dia_annualized * 100,
        dia_sharpe=_sharpe(dia),
        dia_max_drawdown_pct=_max_drawdown(dia) * 100,
        excess_annualized_return_vs_qqq_pct=(
            strategy_annualized - qqq_annualized
        )
        * 100,
        excess_annualized_return_vs_dia_pct=(
            strategy_annualized - dia_annualized
        )
        * 100,
    )


def _training_score(performance: RotationPerformance) -> float:
    benchmark_return = max(
        performance.qqq_annualized_return_pct,
        performance.dia_annualized_return_pct,
    )
    benchmark_sharpe = max(
        performance.qqq_sharpe,
        performance.dia_sharpe,
    )
    benchmark_drawdown = max(
        performance.qqq_max_drawdown_pct,
        performance.dia_max_drawdown_pct,
    )
    return (
        performance.annualized_return_pct
        - benchmark_return
        + 2.0 * (performance.sharpe - benchmark_sharpe)
        - 0.25
        * max(
            performance.max_drawdown_pct - benchmark_drawdown,
            0.0,
        )
    )


def _validation_blockers(
    performance: RotationPerformance,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if performance.periods < 12:
        blockers.append("ROTATION_VALIDATION_HISTORY_INSUFFICIENT")
    if performance.annualized_return_pct <= 0:
        blockers.append("ROTATION_VALIDATION_RETURN_NON_POSITIVE")
    if performance.excess_annualized_return_vs_qqq_pct <= 0:
        blockers.append("ROTATION_VALIDATION_NOT_ABOVE_QQQ")
    if performance.excess_annualized_return_vs_dia_pct <= 0:
        blockers.append("ROTATION_VALIDATION_NOT_ABOVE_DIA")
    if performance.sharpe <= max(
        performance.qqq_sharpe,
        performance.dia_sharpe,
    ):
        blockers.append("ROTATION_VALIDATION_SHARPE_NOT_ABOVE_BENCHMARK")
    if performance.max_drawdown_pct > max(
        performance.qqq_max_drawdown_pct,
        performance.dia_max_drawdown_pct,
    ) + 5.0:
        blockers.append("ROTATION_VALIDATION_DRAWDOWN_EXCESS")
    return tuple(blockers)


def evaluate_rotation_walk_forward(
    *,
    candidates: Sequence[IndexCandidate],
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    benchmark_bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    base_config: UniverseSelectionConfig,
    variants: Sequence[RotationVariant] = DEFAULT_ROTATION_VARIANTS,
    validation_periods: int = 12,
) -> RotationWalkForwardResult:
    if not candidates:
        raise ValueError("candidates must not be empty")
    if not variants:
        raise ValueError("variants must not be empty")
    variant_names = [variant.name for variant in variants]
    if len(set(variant_names)) != len(variant_names):
        raise ValueError("rotation variant names must be unique")
    if validation_periods < 1:
        raise ValueError("validation_periods must be positive")
    benchmark_maps = {
        symbol: _bar_map(bars)
        for symbol, bars in benchmark_bars_by_symbol.items()
    }
    histories_by_symbol: dict[
        str,
        tuple[tuple[date, ...], tuple[DailyBar, ...]],
    ] = {}
    for symbol, bars in bars_by_symbol.items():
        ordered_bars = tuple(
            sorted(bars, key=lambda item: item.timestamp)
        )
        histories_by_symbol[symbol] = (
            tuple(_bar_date(bar) for bar in ordered_bars),
            ordered_bars,
        )
    candidate_maps = {
        symbol: _bar_map(bars)
        for symbol, bars in bars_by_symbol.items()
    }
    common_dates, rebalance_dates = _monthly_rebalance_dates(
        benchmark_maps,
    )
    if len(rebalance_dates) < 2:
        return RotationWalkForwardResult(
            algorithm_version=ROTATION_WALK_FORWARD_VERSION,
            status="BENCHMARK_HISTORY_UNAVAILABLE",
            benchmark_symbols=ROTATION_BENCHMARK_SYMBOLS,
            data_scope="CURRENT_CONSTITUENTS_ONLY",
            survivorship_bias=True,
            validation_periods=validation_periods,
            selected_variant=None,
            selected_variant_validation_passed=False,
            validated_challenger_variant=None,
            automatic_promotion_allowed=False,
            promotion_blockers=(
                "ROTATION_BENCHMARK_HISTORY_UNAVAILABLE",
                "CURRENT_CONSTITUENTS_SURVIVORSHIP_BIAS",
                "ROTATION_FORWARD_OBSERVATIONS_REQUIRED",
            ),
            variants=(),
            selected_variant_periods=(),
            validated_challenger_periods=(),
        )

    raw_periods_by_variant: dict[str, tuple[RotationPeriod, ...]] = {}
    for variant in variants:
        raw_periods_by_variant[variant.name] = tuple(
            _simulate_variant(
                candidates=candidates,
                histories_by_symbol=histories_by_symbol,
                candidate_maps=candidate_maps,
                benchmark_maps=benchmark_maps,
                common_dates=common_dates,
                rebalance_dates=rebalance_dates,
                base_config=base_config,
                variant=variant,
            )
        )

    non_empty_period_dates = [
        {period.entry_date for period in periods}
        for periods in raw_periods_by_variant.values()
        if periods
    ]
    common_period_dates = (
        set.intersection(*non_empty_period_dates)
        if non_empty_period_dates
        else set()
    )
    periods_by_variant = {
        name: tuple(
            period
            for period in periods
            if period.entry_date in common_period_dates
        )
        for name, periods in raw_periods_by_variant.items()
    }

    evaluations: list[RotationVariantEvaluation] = []
    for variant in variants:
        periods = periods_by_variant[variant.name]
        split = max(0, len(periods) - validation_periods)
        training_periods = periods[:split]
        validation_slice = periods[split:]
        training = _performance(training_periods)
        validation = _performance(validation_slice)
        blockers = _validation_blockers(validation)
        evaluations.append(
            RotationVariantEvaluation(
                variant=variant,
                training_score=_training_score(training),
                validation_passed=not blockers,
                validation_blockers=blockers,
                full=_performance(periods),
                training=training,
                validation=validation,
            )
        )

    training_eligible = [
        evaluation
        for evaluation in evaluations
        if evaluation.training.periods >= 12
    ]
    selected = (
        max(
            training_eligible,
            key=lambda evaluation: (
                evaluation.training_score,
                evaluation.variant.name,
            ),
        )
        if training_eligible
        else None
    )
    validated_candidates = [
        evaluation
        for evaluation in evaluations
        if (
            evaluation.validation_passed
            and evaluation.training.periods >= 12
            and evaluation.training_score > 0
        )
    ]
    validated_challenger = (
        max(
            validated_candidates,
            key=lambda evaluation: (
                evaluation.training_score,
                evaluation.variant.name,
            ),
        )
        if validated_candidates
        else None
    )
    promotion_blockers = [
        "CURRENT_CONSTITUENTS_SURVIVORSHIP_BIAS",
        "ROTATION_FORWARD_OBSERVATIONS_REQUIRED",
    ]
    if selected is None:
        promotion_blockers.append("ROTATION_TRAINING_HISTORY_INSUFFICIENT")
    elif validated_challenger is None:
        promotion_blockers.extend(selected.validation_blockers)
    return RotationWalkForwardResult(
        algorithm_version=ROTATION_WALK_FORWARD_VERSION,
        status=(
            "COMPLETE"
            if selected is not None
            else "HISTORY_INSUFFICIENT"
        ),
        benchmark_symbols=ROTATION_BENCHMARK_SYMBOLS,
        data_scope="CURRENT_CONSTITUENTS_ONLY",
        survivorship_bias=True,
        validation_periods=validation_periods,
        selected_variant=selected.variant.name if selected else None,
        selected_variant_validation_passed=(
            selected.validation_passed if selected else False
        ),
        validated_challenger_variant=(
            validated_challenger.variant.name
            if validated_challenger
            else None
        ),
        automatic_promotion_allowed=False,
        promotion_blockers=tuple(dict.fromkeys(promotion_blockers)),
        variants=tuple(evaluations),
        selected_variant_periods=(
            periods_by_variant.get(selected.variant.name, ())
            if selected
            else ()
        ),
        validated_challenger_periods=(
            periods_by_variant.get(
                validated_challenger.variant.name,
                (),
            )
            if validated_challenger
            else ()
        ),
    )
