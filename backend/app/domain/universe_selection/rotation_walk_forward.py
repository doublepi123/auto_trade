from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import asdict, dataclass, replace
from datetime import date
from statistics import mean, stdev
from typing import Literal, Mapping, Sequence

from app.core.market_calendar import get_session
from app.domain.universe_selection.catalog import IndexCandidate
from app.domain.universe_selection.membership_history import (
    IndexMembershipHistory,
)
from app.domain.universe_selection.selector import (
    CandidateInput,
    CandidateSelection,
    DailyBar,
    UniverseSelectionConfig,
    liquidity_spread_proxy_bps,
    select_candidates,
)


ROTATION_WALK_FORWARD_VERSION = "rotation-monthly-open-walk-forward-v5"
ROTATION_BENCHMARK_SYMBOLS = ("QQQ.US", "DIA.US")
_CASH = "__CASH__"
_EXPANDING_VALIDATION_MIN_TRAINING_PERIODS = 12
_EXPANDING_VALIDATION_FOLD_PERIODS = 12


@dataclass(frozen=True)
class RotationVariant:
    name: str
    lookback_bars: int
    skip_bars: int
    sma_bars: int
    max_selected: int
    max_per_risk_group: int
    ranking: Literal[
        "raw_momentum",
        "return_to_variance",
    ] = "raw_momentum"
    weighting: Literal[
        "equal",
        "inverse_volatility",
        "equal_inverse_volatility_blend",
    ] = "equal"
    max_position_weight_pct: float = 100.0
    inverse_volatility_blend_pct: float = 0.0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("rotation variant name must not be empty")
        if self.lookback_bars <= self.skip_bars:
            raise ValueError("lookback_bars must exceed skip_bars")
        if self.skip_bars < 1 or self.sma_bars < 2:
            raise ValueError("skip_bars and sma_bars are invalid")
        if self.max_selected < 1 or self.max_per_risk_group < 1:
            raise ValueError("selection limits must be positive")
        if self.ranking not in {
            "raw_momentum",
            "return_to_variance",
        }:
            raise ValueError("rotation ranking is invalid")
        if self.weighting not in {
            "equal",
            "inverse_volatility",
            "equal_inverse_volatility_blend",
        }:
            raise ValueError("rotation weighting is invalid")
        if (
            not math.isfinite(self.max_position_weight_pct)
            or not 0 < self.max_position_weight_pct <= 100
        ):
            raise ValueError(
                "max_position_weight_pct must be in (0, 100]"
            )
        if (
            not math.isfinite(self.inverse_volatility_blend_pct)
            or not 0 <= self.inverse_volatility_blend_pct <= 100
        ):
            raise ValueError(
                "inverse_volatility_blend_pct must be in [0, 100]"
            )
        if self.weighting == "equal_inverse_volatility_blend":
            if not 0 < self.inverse_volatility_blend_pct < 100:
                raise ValueError(
                    "blended weighting requires an inverse-volatility "
                    "share in (0, 100)"
                )
        elif self.inverse_volatility_blend_pct != 0:
            raise ValueError(
                "inverse_volatility_blend_pct is only valid for "
                "blended weighting"
            )


DIVERSIFIED_ROTATION_VARIANT = RotationVariant(
    name="diversified_top8_12_1",
    lookback_bars=252,
    skip_bars=21,
    sma_bars=200,
    max_selected=8,
    max_per_risk_group=1,
)

CONCENTRATED_ROTATION_VARIANT = RotationVariant(
    name="concentrated_top6_12_1",
    lookback_bars=252,
    skip_bars=21,
    sma_bars=200,
    max_selected=6,
    max_per_risk_group=2,
)

DIVERSIFIED_INVERSE_VOLATILITY_VARIANT = RotationVariant(
    name="diversified_top8_12_1_inverse_vol_25",
    lookback_bars=252,
    skip_bars=21,
    sma_bars=200,
    max_selected=8,
    max_per_risk_group=1,
    weighting="inverse_volatility",
    max_position_weight_pct=25.0,
)

DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT = RotationVariant(
    name="diversified_top8_12_1_eq75_iv25_cap15",
    lookback_bars=252,
    skip_bars=21,
    sma_bars=200,
    max_selected=8,
    max_per_risk_group=1,
    weighting="equal_inverse_volatility_blend",
    max_position_weight_pct=15.0,
    inverse_volatility_blend_pct=25.0,
)

RETURN_TO_VARIANCE_ROTATION_VARIANT = RotationVariant(
    name="diversified_top8_12_1_return_to_variance",
    lookback_bars=252,
    skip_bars=21,
    sma_bars=200,
    max_selected=8,
    max_per_risk_group=1,
    ranking="return_to_variance",
)


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
    CONCENTRATED_ROTATION_VARIANT,
    RotationVariant(
        name="faster_top8_6_1",
        lookback_bars=126,
        skip_bars=21,
        sma_bars=126,
        max_selected=8,
        max_per_risk_group=2,
    ),
    DIVERSIFIED_ROTATION_VARIANT,
    DIVERSIFIED_INVERSE_VOLATILITY_VARIANT,
    DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT,
    RETURN_TO_VARIANCE_ROTATION_VARIANT,
)


@dataclass(frozen=True)
class RotationPeriod:
    signal_date: date
    entry_date: date
    exit_date: date
    selected_symbols: tuple[str, ...]
    target_weights_pct: tuple[tuple[str, float], ...]
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
class RotationValidationFold:
    fold: int
    training_periods: int
    training_end_date: date
    validation_periods: int
    validation_start_date: date
    validation_end_date: date
    training_score: float
    passed: bool
    blockers: tuple[str, ...]
    performance: RotationPerformance


@dataclass(frozen=True)
class RotationVariantEvaluation:
    variant: RotationVariant
    training_score: float
    validation_passed: bool
    validation_blockers: tuple[str, ...]
    expanding_validation_passed: bool
    expanding_validation_blockers: tuple[str, ...]
    expanding_folds_passed: int
    expanding_folds_total: int
    expanding_validation: RotationPerformance
    expanding_folds: tuple[RotationValidationFold, ...]
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
    expanding_validation_min_training_periods: int
    expanding_validation_fold_periods: int
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
        raw_variants = payload["variants"]
        if isinstance(raw_variants, (list, tuple)):
            for raw_variant, evaluation in zip(
                raw_variants,
                self.variants,
            ):
                if not isinstance(raw_variant, dict):
                    continue
                raw_folds = raw_variant.get("expanding_folds")
                if not isinstance(raw_folds, (list, tuple)):
                    continue
                for raw_fold, fold in zip(
                    raw_folds,
                    evaluation.expanding_folds,
                ):
                    if not isinstance(raw_fold, dict):
                        continue
                    raw_fold["training_end_date"] = (
                        fold.training_end_date.isoformat()
                    )
                    raw_fold["validation_start_date"] = (
                        fold.validation_start_date.isoformat()
                    )
                    raw_fold["validation_end_date"] = (
                        fold.validation_end_date.isoformat()
                    )
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
    membership_history: IndexMembershipHistory | None = None,
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
        errors: list[str] = []
        if (
            membership_history is not None
            and not membership_history.is_active(
                candidate,
                signal_date,
            )
        ):
            errors.append("INDEX_MEMBERSHIP_INACTIVE")
        if not bars or dates[end - 1] != signal_date:
            errors.append("DATA_STALE_SESSION_DATE")
        inputs.append(
            CandidateInput(
                candidate=candidate,
                completed_daily_bars=bars,
                bid=None,
                ask=None,
                estimated_spread_bps=liquidity_spread_proxy_bps(bars),
                data_errors=tuple(errors),
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


def _capped_weights(
    raw_weights: Mapping[str, float],
    *,
    max_position_weight_pct: float,
    total_weight: float = 1.0,
) -> dict[str, float]:
    if (
        not math.isfinite(total_weight)
        or total_weight < 0
        or total_weight > 1 + 1e-12
    ):
        raise ValueError("total_weight must be in [0, 1]")
    total_weight = min(1.0, max(0.0, total_weight))
    if not raw_weights:
        return {_CASH: 1.0}
    cap = max_position_weight_pct / 100
    remaining_assets = set(raw_weights)
    weights: dict[str, float] = {}
    remaining_weight = total_weight
    while remaining_assets and remaining_weight > 1e-12:
        raw_total = sum(
            raw_weights[symbol]
            for symbol in remaining_assets
        )
        if not math.isfinite(raw_total) or raw_total <= 0:
            break
        proposed = {
            symbol: (
                remaining_weight
                * raw_weights[symbol]
                / raw_total
            )
            for symbol in remaining_assets
        }
        capped = sorted(
            symbol
            for symbol, weight in proposed.items()
            if weight > cap
        )
        if not capped:
            weights.update(proposed)
            remaining_weight = 0.0
            break
        for symbol in capped:
            weights[symbol] = cap
            remaining_assets.remove(symbol)
            remaining_weight = max(
                0.0,
                remaining_weight - cap,
            )
    allocated = sum(weights.values())
    return weights | {_CASH: max(0.0, 1.0 - allocated)}


def rotation_target_weights(
    rows: Sequence[CandidateSelection],
    variant: RotationVariant,
) -> dict[str, float]:
    equal_raw = {
        row.candidate.symbol: 1.0
        for row in rows
    }
    if variant.weighting == "equal":
        return _capped_weights(
            equal_raw,
            max_position_weight_pct=(
                variant.max_position_weight_pct
            ),
        )

    inverse_raw: dict[str, float] = {}
    for row in rows:
        volatility = row.metrics.realized_vol_20d
        if (
            volatility is None
            or not math.isfinite(volatility)
            or volatility <= 0
        ):
            raise ValueError(
                "inverse-volatility weighting requires "
                "positive realized volatility"
            )
        inverse_raw[row.candidate.symbol] = 1.0 / volatility
    inverse_weights = _capped_weights(
        inverse_raw,
        max_position_weight_pct=(
            variant.max_position_weight_pct
        ),
    )
    if variant.weighting == "inverse_volatility":
        return inverse_weights

    equal_weights = _capped_weights(
        equal_raw,
        max_position_weight_pct=100.0,
    )
    inverse_share = variant.inverse_volatility_blend_pct / 100
    equal_share = 1.0 - inverse_share
    assets = (set(equal_weights) | set(inverse_weights)) - {_CASH}
    blended_assets = {
        symbol: (
            equal_share * equal_weights.get(symbol, 0.0)
            + inverse_share * inverse_weights.get(symbol, 0.0)
        )
        for symbol in assets
    }
    return _capped_weights(
        blended_assets,
        max_position_weight_pct=(
            variant.max_position_weight_pct
        ),
        total_weight=sum(blended_assets.values()),
    )


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
    membership_history: IndexMembershipHistory | None,
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
        rotation_ranking=variant.ranking,
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
                membership_history=membership_history,
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
        tradable_rows: list[CandidateSelection] = []
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
            tradable_rows.append(row)
            returns[symbol] = (
                float(exit_bar.open) / entry_price - 1.0
                if exit_bar is not None
                else -1.0
            )
        new_weights = rotation_target_weights(
            tradable_rows,
            variant,
        )
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
                selected_symbols=tuple(
                    row.candidate.symbol
                    for row in tradable_rows
                ),
                target_weights_pct=tuple(
                    (
                        row.candidate.symbol,
                        new_weights.get(
                            row.candidate.symbol,
                            0.0,
                        )
                        * 100,
                    )
                    for row in tradable_rows
                ),
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
    *,
    minimum_periods: int = 12,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if performance.periods < minimum_periods:
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


def _expanding_validation_folds(
    periods: Sequence[RotationPeriod],
    *,
    minimum_training_periods: int,
    fold_periods: int,
) -> tuple[
    tuple[RotationValidationFold, ...],
    RotationPerformance,
    tuple[str, ...],
]:
    folds: list[RotationValidationFold] = []
    validation_periods: list[RotationPeriod] = []
    validation_start = minimum_training_periods
    fold_number = 1
    while validation_start < len(periods):
        validation_end = min(
            len(periods),
            validation_start + fold_periods,
        )
        validation_slice = periods[
            validation_start:validation_end
        ]
        minimum_fold_periods = min(
            fold_periods,
            max(6, fold_periods // 2),
        )
        if len(validation_slice) < minimum_fold_periods:
            break
        training_slice = periods[:validation_start]
        training_performance = _performance(training_slice)
        validation_performance = _performance(validation_slice)
        blockers = _validation_blockers(
            validation_performance,
            minimum_periods=len(validation_slice),
        )
        folds.append(
            RotationValidationFold(
                fold=fold_number,
                training_periods=len(training_slice),
                training_end_date=training_slice[-1].exit_date,
                validation_periods=len(validation_slice),
                validation_start_date=(
                    validation_slice[0].entry_date
                ),
                validation_end_date=(
                    validation_slice[-1].exit_date
                ),
                training_score=_training_score(
                    training_performance
                ),
                passed=not blockers,
                blockers=blockers,
                performance=validation_performance,
            )
        )
        validation_periods.extend(validation_slice)
        fold_number += 1
        validation_start = validation_end

    combined = _performance(validation_periods)
    combined_blockers = list(
        _validation_blockers(
            combined,
            minimum_periods=(
                min(24, len(validation_periods))
                if validation_periods
                else 1
            ),
        )
    )
    if len(folds) < 2:
        combined_blockers.append(
            "ROTATION_EXPANDING_FOLDS_INSUFFICIENT"
        )
    folds_passed = sum(fold.passed for fold in folds)
    if folds and folds_passed * 3 < len(folds) * 2:
        combined_blockers.append(
            "ROTATION_EXPANDING_FOLD_STABILITY_INSUFFICIENT"
        )
    return (
        tuple(folds),
        combined,
        tuple(dict.fromkeys(combined_blockers)),
    )


def evaluate_rotation_walk_forward(
    *,
    candidates: Sequence[IndexCandidate],
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    benchmark_bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    base_config: UniverseSelectionConfig,
    variants: Sequence[RotationVariant] = DEFAULT_ROTATION_VARIANTS,
    validation_periods: int = 12,
    expanding_validation_min_training_periods: int = (
        _EXPANDING_VALIDATION_MIN_TRAINING_PERIODS
    ),
    expanding_validation_fold_periods: int = (
        _EXPANDING_VALIDATION_FOLD_PERIODS
    ),
    membership_history: IndexMembershipHistory | None = None,
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
    if expanding_validation_min_training_periods < 12:
        raise ValueError(
            "expanding validation needs at least 12 training periods"
        )
    if expanding_validation_fold_periods < 6:
        raise ValueError(
            "expanding validation folds need at least 6 periods"
        )
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
    data_scope = (
        "POINT_IN_TIME_CURRENT_CATALOG"
        if membership_history is not None
        else "CURRENT_CONSTITUENTS_ONLY"
    )
    scope_blockers = [
        (
            "HISTORICAL_CONSTITUENTS_OMITTED"
            if membership_history is not None
            else "CURRENT_CONSTITUENTS_SURVIVORSHIP_BIAS"
        ),
    ]
    if membership_history is not None:
        coverage = membership_history.coverage(candidates)
        if (
            coverage.snapshot_only_symbols
            or coverage.missing_symbols
        ):
            scope_blockers.append(
                "POINT_IN_TIME_MEMBERSHIP_HISTORY_PARTIAL"
            )
    if len(rebalance_dates) < 2:
        return RotationWalkForwardResult(
            algorithm_version=ROTATION_WALK_FORWARD_VERSION,
            status="BENCHMARK_HISTORY_UNAVAILABLE",
            benchmark_symbols=ROTATION_BENCHMARK_SYMBOLS,
            data_scope=data_scope,
            survivorship_bias=True,
            validation_periods=validation_periods,
            expanding_validation_min_training_periods=(
                expanding_validation_min_training_periods
            ),
            expanding_validation_fold_periods=(
                expanding_validation_fold_periods
            ),
            selected_variant=None,
            selected_variant_validation_passed=False,
            validated_challenger_variant=None,
            automatic_promotion_allowed=False,
            promotion_blockers=(
                "ROTATION_BENCHMARK_HISTORY_UNAVAILABLE",
                *scope_blockers,
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
                membership_history=membership_history,
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
        (
            expanding_folds,
            expanding_validation,
            expanding_blockers,
        ) = _expanding_validation_folds(
            periods,
            minimum_training_periods=(
                expanding_validation_min_training_periods
            ),
            fold_periods=(
                expanding_validation_fold_periods
            ),
        )
        evaluations.append(
            RotationVariantEvaluation(
                variant=variant,
                training_score=_training_score(training),
                validation_passed=not blockers,
                validation_blockers=blockers,
                expanding_validation_passed=(
                    not expanding_blockers
                ),
                expanding_validation_blockers=(
                    expanding_blockers
                ),
                expanding_folds_passed=sum(
                    fold.passed
                    for fold in expanding_folds
                ),
                expanding_folds_total=len(expanding_folds),
                expanding_validation=expanding_validation,
                expanding_folds=expanding_folds,
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
            and evaluation.expanding_validation_passed
            and evaluation.training.periods >= 12
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
        *scope_blockers,
        "ROTATION_FORWARD_OBSERVATIONS_REQUIRED",
    ]
    if selected is None:
        promotion_blockers.append("ROTATION_TRAINING_HISTORY_INSUFFICIENT")
    elif validated_challenger is None:
        promotion_blockers.extend(selected.validation_blockers)
        promotion_blockers.extend(
            selected.expanding_validation_blockers
        )
    return RotationWalkForwardResult(
        algorithm_version=ROTATION_WALK_FORWARD_VERSION,
        status=(
            "COMPLETE"
            if selected is not None
            else "HISTORY_INSUFFICIENT"
        ),
        benchmark_symbols=ROTATION_BENCHMARK_SYMBOLS,
        data_scope=data_scope,
        survivorship_bias=True,
        validation_periods=validation_periods,
        expanding_validation_min_training_periods=(
            expanding_validation_min_training_periods
        ),
        expanding_validation_fold_periods=(
            expanding_validation_fold_periods
        ),
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
