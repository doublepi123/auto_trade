from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from statistics import mean, stdev
from typing import Protocol, Sequence

from app.core.holiday_calendar import is_market_closed
from app.core.market_calendar import get_session
from app.domain.universe_selection.catalog import IndexCandidate


UNIVERSE_ALGORITHM_VERSION = "index-liquidity-opportunity-v10"
ROTATION_ALGORITHM_VERSION = (
    "index-momentum-12-1-diversified-monthly-shadow-v3"
)
_DAILY_BAR_FINALIZATION_DELAY = timedelta(minutes=15)


class DailyBar(Protocol):
    @property
    def timestamp(self) -> datetime: ...

    @property
    def open(self) -> float: ...

    @property
    def high(self) -> float: ...

    @property
    def low(self) -> float: ...

    @property
    def close(self) -> float: ...

    @property
    def volume(self) -> float: ...

    @property
    def turnover(self) -> float: ...


@dataclass(frozen=True)
class UniverseSelectionConfig:
    max_selected: int = 12
    max_per_sector: int = 2
    rotation_max_selected: int = 8
    rotation_max_per_risk_group: int = 1
    rotation_lookback_bars: int = 252
    rotation_skip_bars: int = 21
    rotation_sma_bars: int = 200
    min_completed_bars: int = 21
    min_price: float = 10.0
    min_avg_dollar_volume: float = 500_000_000.0
    max_relative_spread_bps: float = 15.0
    min_realized_vol_20d: float = 0.15
    max_realized_vol_20d: float = 1.20
    min_atr_pct_14d: float = 0.75
    max_atr_pct_14d: float = 8.0
    round_trip_fee_bps: float = 10.0
    round_trip_slippage_bps: float = 4.0

    def __post_init__(self) -> None:
        if self.max_selected < 1:
            raise ValueError("max_selected must be positive")
        if self.max_per_sector < 1:
            raise ValueError("max_per_sector must be positive")
        if self.rotation_max_selected < 1:
            raise ValueError("rotation_max_selected must be positive")
        if self.rotation_max_per_risk_group < 1:
            raise ValueError(
                "rotation_max_per_risk_group must be positive"
            )
        if self.rotation_skip_bars < 1:
            raise ValueError("rotation_skip_bars must be positive")
        if self.rotation_lookback_bars <= self.rotation_skip_bars:
            raise ValueError(
                "rotation_lookback_bars must exceed rotation_skip_bars"
            )
        if self.rotation_sma_bars < 2:
            raise ValueError("rotation_sma_bars must be at least 2")
        if self.min_completed_bars < 21:
            raise ValueError("min_completed_bars must be at least 21")
        positive_values = (
            self.min_price,
            self.min_avg_dollar_volume,
            self.max_relative_spread_bps,
            self.min_atr_pct_14d,
            self.max_atr_pct_14d,
        )
        if any(not math.isfinite(value) or value <= 0 for value in positive_values):
            raise ValueError("price, liquidity, spread, and ATR bounds must be positive")
        if not 0 < self.min_realized_vol_20d < self.max_realized_vol_20d:
            raise ValueError("realized-volatility bounds are invalid")
        if self.min_atr_pct_14d >= self.max_atr_pct_14d:
            raise ValueError("ATR bounds are invalid")
        if self.round_trip_fee_bps < 0 or self.round_trip_slippage_bps < 0:
            raise ValueError("cost assumptions must not be negative")


@dataclass(frozen=True)
class CandidateInput:
    candidate: IndexCandidate
    completed_daily_bars: Sequence[DailyBar]
    bid: float | None
    ask: float | None
    estimated_spread_bps: float | None = None
    data_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateMetrics:
    price: float | None = None
    avg_dollar_volume: float | None = None
    relative_spread_bps: float | None = None
    realized_vol_20d: float | None = None
    atr_pct_14d: float | None = None
    momentum_5d_pct: float | None = None
    trend_efficiency_10d: float | None = None
    opportunity_to_cost_ratio: float | None = None


@dataclass(frozen=True)
class RotationSelectionEvidence:
    algorithm_version: str = ROTATION_ALGORITHM_VERSION
    lookback_bars: int = 252
    skip_bars: int = 21
    sma_bars: int = 200
    momentum_pct: float | None = None
    sma_price: float | None = None
    above_sma: bool | None = None
    eligible: bool = False
    selected: bool = False
    rank: int | None = None
    score: float = 0.0
    exclusion_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateSelection:
    candidate: IndexCandidate
    metrics: CandidateMetrics
    exclusion_reasons: tuple[str, ...]
    selected: bool = False
    rank: int | None = None
    score: float = 0.0
    rotation: RotationSelectionEvidence = field(
        default_factory=RotationSelectionEvidence,
    )

    @property
    def evaluable(self) -> bool:
        return not any(reason.startswith("DATA_") for reason in self.exclusion_reasons)


def _finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _dollar_volume(bar: DailyBar) -> float:
    turnover = float(getattr(bar, "turnover", 0.0) or 0.0)
    if math.isfinite(turnover) and turnover > 0:
        return turnover
    return float(bar.close) * float(bar.volume)


def liquidity_spread_proxy_bps(
    bars: Sequence[DailyBar],
) -> float | None:
    """Estimate a stable T-1 cost proxy from completed dollar volume.

    Live BBO belongs to the intraday quant stage. The daily universe must be
    reproducible, so it uses a bounded, monotonic liquidity proxy instead:
    2.5 bps at $500m ADV, tightening with the square root of liquidity.
    """
    complete = sorted(bars, key=lambda bar: bar.timestamp)[-20:]
    if not complete:
        return None
    dollar_volumes = [_dollar_volume(bar) for bar in complete]
    if any(
        not math.isfinite(value) or value <= 0
        for value in dollar_volumes
    ):
        return None
    average = mean(dollar_volumes)
    estimate = 2.5 * math.sqrt(500_000_000.0 / average)
    return min(10.0, max(0.5, estimate))


def _candidate_metrics(
    item: CandidateInput,
    config: UniverseSelectionConfig,
) -> tuple[CandidateMetrics, tuple[str, ...]]:
    if item.data_errors:
        return CandidateMetrics(), item.data_errors
    bars = sorted(item.completed_daily_bars, key=lambda bar: bar.timestamp)
    reasons: list[str] = []
    if len(bars) < config.min_completed_bars:
        return CandidateMetrics(), ("DATA_INSUFFICIENT_DAILY_BARS",)

    bars = bars[-config.min_completed_bars :]
    values = [
        value
        for bar in bars
        for value in (bar.open, bar.high, bar.low, bar.close, bar.volume)
    ]
    if any(not math.isfinite(float(value)) for value in values):
        return CandidateMetrics(), ("DATA_NON_FINITE_DAILY_BAR",)
    if any(
        min(bar.open, bar.high, bar.low, bar.close) <= 0
        or bar.volume < 0
        or bar.high < max(bar.open, bar.close, bar.low)
        or bar.low > min(bar.open, bar.close, bar.high)
        for bar in bars
    ):
        return CandidateMetrics(), ("DATA_INVALID_DAILY_BAR",)

    price = float(bars[-1].close)
    dollar_volumes = [_dollar_volume(bar) for bar in bars[-20:]]
    avg_dollar_volume = mean(dollar_volumes)

    closes = [float(bar.close) for bar in bars[-21:]]
    returns = [math.log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]
    realized_vol = stdev(returns) * math.sqrt(252) if len(returns) >= 2 else 0.0

    true_ranges: list[float] = []
    atr_bars = bars[-15:]
    for index in range(1, len(atr_bars)):
        current = atr_bars[index]
        previous = atr_bars[index - 1]
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    atr_pct = mean(true_ranges[-14:]) / price * 100 if true_ranges else 0.0
    momentum_5d = (price / float(bars[-6].close) - 1.0) * 100

    efficiency_closes = [float(bar.close) for bar in bars[-11:]]
    path = sum(
        abs(efficiency_closes[index] - efficiency_closes[index - 1])
        for index in range(1, len(efficiency_closes))
    )
    trend_efficiency = (
        abs(efficiency_closes[-1] - efficiency_closes[0]) / path
        if path > 0
        else 0.0
    )

    if item.estimated_spread_bps is not None:
        relative_spread_bps = float(item.estimated_spread_bps)
        if (
            not math.isfinite(relative_spread_bps)
            or relative_spread_bps < 0
        ):
            relative_spread_bps = None
            reasons.append("DATA_INVALID_SPREAD_PROXY")
    else:
        bid = float(item.bid) if item.bid is not None else 0.0
        ask = float(item.ask) if item.ask is not None else 0.0
        if (
            not _finite_positive(bid)
            or not _finite_positive(ask)
            or ask < bid
        ):
            relative_spread_bps = None
            reasons.append("DATA_INVALID_QUOTE")
        else:
            mid = (bid + ask) / 2
            relative_spread_bps = (ask - bid) / mid * 10_000

    cost_bps = (
        (relative_spread_bps or 0.0)
        + config.round_trip_fee_bps
        + config.round_trip_slippage_bps
    )
    opportunity_to_cost = atr_pct * 100 / cost_bps if cost_bps > 0 else None
    metrics = CandidateMetrics(
        price=price,
        avg_dollar_volume=avg_dollar_volume,
        relative_spread_bps=relative_spread_bps,
        realized_vol_20d=realized_vol,
        atr_pct_14d=atr_pct,
        momentum_5d_pct=momentum_5d,
        trend_efficiency_10d=trend_efficiency,
        opportunity_to_cost_ratio=opportunity_to_cost,
    )

    if price < config.min_price:
        reasons.append("PRICE_BELOW_MINIMUM")
    if avg_dollar_volume < config.min_avg_dollar_volume:
        reasons.append("DOLLAR_VOLUME_BELOW_MINIMUM")
    if relative_spread_bps is not None and relative_spread_bps > config.max_relative_spread_bps:
        reasons.append("SPREAD_ABOVE_MAXIMUM")
    if not config.min_realized_vol_20d <= realized_vol <= config.max_realized_vol_20d:
        reasons.append("REALIZED_VOL_OUTSIDE_RANGE")
    if not config.min_atr_pct_14d <= atr_pct <= config.max_atr_pct_14d:
        reasons.append("ATR_OUTSIDE_RANGE")
    return metrics, tuple(reasons)


def _percentile_ranks(
    values: dict[str, float],
    *,
    higher_is_better: bool,
) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(
        values.items(),
        key=lambda pair: (pair[1], pair[0]),
        reverse=higher_is_better,
    )
    if len(ordered) == 1:
        return {ordered[0][0]: 1.0}
    denominator = len(ordered) - 1
    return {
        symbol: 1.0 - index / denominator
        for index, (symbol, _) in enumerate(ordered)
    }


def _rotation_evidence(
    inputs: Sequence[CandidateInput],
    evaluated: Sequence[CandidateSelection],
    config: UniverseSelectionConfig,
) -> dict[str, RotationSelectionEvidence]:
    input_by_symbol = {
        item.candidate.symbol: item
        for item in inputs
    }
    evidence_by_symbol: dict[str, RotationSelectionEvidence] = {}
    required_bars = max(
        config.rotation_lookback_bars + 1,
        config.rotation_sma_bars,
    )
    for row in evaluated:
        symbol = row.candidate.symbol
        item = input_by_symbol[symbol]
        reasons = list(row.exclusion_reasons)
        bars = sorted(
            item.completed_daily_bars,
            key=lambda bar: bar.timestamp,
        )
        momentum_pct: float | None = None
        sma_price: float | None = None
        above_sma: bool | None = None
        if len(bars) < required_bars:
            reasons.append("ROTATION_HISTORY_INSUFFICIENT")
        else:
            closes = [
                float(bar.close)
                for bar in bars[-required_bars:]
            ]
            if any(
                not math.isfinite(value) or value <= 0
                for value in closes
            ):
                reasons.append("ROTATION_DATA_INVALID_CLOSE")
            else:
                formation_start = closes[
                    -(config.rotation_lookback_bars + 1)
                ]
                formation_end = closes[
                    -(config.rotation_skip_bars + 1)
                ]
                momentum_pct = (
                    formation_end / formation_start - 1.0
                ) * 100
                sma_price = mean(closes[-config.rotation_sma_bars :])
                above_sma = closes[-1] > sma_price
                if momentum_pct <= 0:
                    reasons.append("ROTATION_NON_POSITIVE_MOMENTUM")
                if not above_sma:
                    reasons.append("ROTATION_BELOW_SMA")
        evidence_by_symbol[symbol] = RotationSelectionEvidence(
            lookback_bars=config.rotation_lookback_bars,
            skip_bars=config.rotation_skip_bars,
            sma_bars=config.rotation_sma_bars,
            momentum_pct=momentum_pct,
            sma_price=sma_price,
            above_sma=above_sma,
            eligible=not reasons,
            exclusion_reasons=tuple(reasons),
        )

    eligible = [
        row
        for row in evaluated
        if evidence_by_symbol[row.candidate.symbol].eligible
    ]
    eligible.sort(
        key=lambda row: (
            -(
                evidence_by_symbol[
                    row.candidate.symbol
                ].momentum_pct
                or 0.0
            ),
            row.candidate.symbol,
        )
    )
    scores = _percentile_ranks(
        {
            row.candidate.symbol: (
                evidence_by_symbol[
                    row.candidate.symbol
                ].momentum_pct
                or 0.0
            )
            for row in eligible
        },
        higher_is_better=True,
    )
    selected_symbols: list[str] = []
    risk_group_counts: dict[str, int] = {}
    for row in eligible:
        symbol = row.candidate.symbol
        evidence = evidence_by_symbol[symbol]
        reasons = list(evidence.exclusion_reasons)
        risk_group = row.candidate.risk_group
        if (
            risk_group_counts.get(risk_group, 0)
            >= config.rotation_max_per_risk_group
        ):
            reasons.append("ROTATION_RISK_GROUP_CAP")
        elif len(selected_symbols) >= config.rotation_max_selected:
            reasons.append("ROTATION_BELOW_SELECTION_CUTOFF")
        else:
            selected_symbols.append(symbol)
            risk_group_counts[risk_group] = (
                risk_group_counts.get(risk_group, 0) + 1
            )
        evidence_by_symbol[symbol] = replace(
            evidence,
            selected=symbol in selected_symbols,
            rank=(
                selected_symbols.index(symbol) + 1
                if symbol in selected_symbols
                else None
            ),
            score=100 * scores[symbol],
            exclusion_reasons=tuple(reasons),
        )
    return evidence_by_symbol


def select_candidates(
    inputs: Sequence[CandidateInput],
    config: UniverseSelectionConfig | None = None,
) -> list[CandidateSelection]:
    selection_config = config or UniverseSelectionConfig()
    evaluated: list[CandidateSelection] = []
    for item in inputs:
        metrics, reasons = _candidate_metrics(item, selection_config)
        evaluated.append(
            CandidateSelection(
                candidate=item.candidate,
                metrics=metrics,
                exclusion_reasons=reasons,
            )
        )

    eligible = [row for row in evaluated if not row.exclusion_reasons]
    liquidity = _percentile_ranks(
        {
            row.candidate.symbol: math.log10(row.metrics.avg_dollar_volume or 1.0)
            for row in eligible
        },
        higher_is_better=True,
    )
    spread = _percentile_ranks(
        {
            row.candidate.symbol: row.metrics.relative_spread_bps or 0.0
            for row in eligible
        },
        higher_is_better=False,
    )
    opportunity = _percentile_ranks(
        {
            row.candidate.symbol: row.metrics.opportunity_to_cost_ratio or 0.0
            for row in eligible
        },
        higher_is_better=True,
    )
    momentum = _percentile_ranks(
        {
            row.candidate.symbol: row.metrics.momentum_5d_pct or 0.0
            for row in eligible
        },
        higher_is_better=True,
    )
    mean_reversion_fit = _percentile_ranks(
        {
            row.candidate.symbol: row.metrics.trend_efficiency_10d or 0.0
            for row in eligible
        },
        higher_is_better=False,
    )

    scored: dict[str, float] = {}
    for row in eligible:
        symbol = row.candidate.symbol
        scored[symbol] = 100 * (
            0.30 * liquidity[symbol]
            + 0.25 * spread[symbol]
            + 0.25 * opportunity[symbol]
            + 0.10 * momentum[symbol]
            + 0.10 * mean_reversion_fit[symbol]
        )

    eligible.sort(key=lambda row: (-scored[row.candidate.symbol], row.candidate.symbol))
    selected_symbols: set[str] = set()
    risk_group_counts: dict[str, int] = {}
    for row in eligible:
        risk_group = row.candidate.risk_group
        if (
            risk_group_counts.get(risk_group, 0)
            >= selection_config.max_per_sector
        ):
            continue
        selected_symbols.add(row.candidate.symbol)
        risk_group_counts[risk_group] = (
            risk_group_counts.get(risk_group, 0) + 1
        )
        if len(selected_symbols) >= selection_config.max_selected:
            break

    ranked_symbols = [
        row.candidate.symbol
        for row in eligible
        if row.candidate.symbol in selected_symbols
    ]
    rank_by_symbol = {
        symbol: index + 1 for index, symbol in enumerate(ranked_symbols)
    }

    rotation_by_symbol = _rotation_evidence(
        inputs,
        evaluated,
        selection_config,
    )
    result: list[CandidateSelection] = []
    for row in evaluated:
        symbol = row.candidate.symbol
        reasons = list(row.exclusion_reasons)
        if not reasons and symbol not in selected_symbols:
            if (
                risk_group_counts.get(row.candidate.risk_group, 0)
                >= selection_config.max_per_sector
            ):
                reasons.append("SECTOR_CAP")
            else:
                reasons.append("BELOW_SELECTION_CUTOFF")
        result.append(
            CandidateSelection(
                candidate=row.candidate,
                metrics=row.metrics,
                exclusion_reasons=tuple(reasons),
                selected=symbol in selected_symbols,
                rank=rank_by_symbol.get(symbol),
                score=scored.get(symbol, 0.0),
                rotation=rotation_by_symbol[symbol],
            )
        )
    return sorted(
        result,
        key=lambda row: (
            not row.selected,
            row.rank or 10_000,
            -row.score,
            row.candidate.symbol,
        ),
    )


def completed_daily_bars(
    bars: Sequence[DailyBar],
    *,
    market: str,
    now: datetime | None = None,
) -> list[DailyBar]:
    """Keep only candles whose exchange session has fully settled.

    Longbridge labels a US daily candle at local midnight and exposes today's
    row during the session. Treating that timestamp as a completed bar would
    leak partial same-day OHLCV into the selector. After the official close,
    a short finalization delay lets the completed session feed the next run
    without imposing an unnecessary extra trading-day lag.
    """
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    session = get_session(market)
    completed_through = latest_closed_session_date(
        market=market,
        now=observed_at,
    )
    return sorted(
        (
            bar
            for bar in bars
            if session.local(bar.timestamp).date() <= completed_through
        ),
        key=lambda bar: bar.timestamp,
    )


def latest_closed_session_date(
    *,
    market: str,
    now: datetime | None = None,
) -> date:
    """Return the latest session date whose daily candle should be final."""
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    session = get_session(market)
    local = session.local(observed_at)
    candidate = local.date()
    is_session_day = (
        candidate.weekday() < 5
        and not is_market_closed(session.code, candidate)
    )
    if is_session_day:
        close_at = datetime.combine(
            candidate,
            session.close_time(candidate),
            tzinfo=session.timezone,
        )
        if local >= close_at + _DAILY_BAR_FINALIZATION_DELAY:
            return candidate
        candidate -= timedelta(days=1)

    for _ in range(14):
        if (
            candidate.weekday() < 5
            and not is_market_closed(session.code, candidate)
        ):
            return candidate
        candidate -= timedelta(days=1)
    raise RuntimeError(f"no completed {session.code} session in 14 days")


def latest_complete_session_date(
    bars: Sequence[DailyBar],
    *,
    market: str,
    now: datetime | None = None,
) -> date | None:
    complete = completed_daily_bars(bars, market=market, now=now)
    if not complete:
        return None
    return get_session(market).local(complete[-1].timestamp).date()
