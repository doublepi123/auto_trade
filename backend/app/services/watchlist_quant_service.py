from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol, Sequence

from sqlalchemy.orm import Session

from app.core.broker import BrokerCandle, Quote
from app.domain.universe_selection import DailyBar, completed_daily_bars
from app.models import WatchlistItem, WatchlistScore
from app.services.watchlist_score_service import WatchlistScoreService

logger = logging.getLogger("auto_trade.watchlist_quant_service")

_MIN_DAILY_BARS = 60
_MIN_INTRADAY_BARS = 300
_DAILY_COUNT = 120
_INTRADAY_COUNT = 1000
_MIN_DOLLAR_VOLUME = 100_000_000.0
_MAX_INTRADAY_GAP = timedelta(minutes=7)


class WatchlistMarketDataProvider(Protocol):
    def get_quotes(self, symbols: list[str]) -> list[Quote]: ...

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]: ...


@dataclass(frozen=True)
class WatchlistQuantMetrics:
    symbol: str
    market: str
    daily_bars: int
    intraday_bars: int
    last_price: float
    median_daily_dollar_volume: float
    spread_bps: float | None
    atr_pct: float
    annualized_volatility_pct: float
    return_20d_pct: float
    drawdown_60d_pct: float
    intraday_autocorrelation: float
    intraday_reversal_rate: float
    intraday_efficiency: float
    horizon_move_p75_bps: float
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class WatchlistQuantScore:
    score: float
    confidence: float
    recommended_action: str
    rationale: str


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _finite_positive(
    value: float | int | Decimal | str | None,
) -> float | None:
    try:
        candidate = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return None
    if not math.isfinite(candidate) or candidate <= 0:
        return None
    return candidate


def _log_returns(values: Sequence[float]) -> list[float]:
    return [
        math.log(current / previous)
        for previous, current in zip(values, values[1:])
        if previous > 0 and current > 0
    ]


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * _clamp(quantile)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _intraday_close_segments(
    bars: Sequence[DailyBar],
    *,
    market: str,
) -> list[list[float]]:
    """Split intraday closes at exchange-day and missing-bar boundaries."""
    from app.core.market_calendar import get_session

    session = get_session(market)
    segments: list[list[float]] = []
    current: list[float] = []
    previous_at: datetime | None = None
    previous_day: date | None = None
    for candle in sorted(bars, key=lambda item: item.timestamp):
        close = _finite_positive(candle.close)
        if close is None:
            if current:
                segments.append(current)
            current = []
            previous_at = None
            previous_day = None
            continue
        timestamp = candle.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)
        market_day = session.local(timestamp).date()
        contiguous = (
            previous_at is not None
            and previous_day == market_day
            and timedelta(0) < timestamp - previous_at <= _MAX_INTRADAY_GAP
        )
        if not contiguous:
            if current:
                segments.append(current)
            current = []
        current.append(close)
        previous_at = timestamp
        previous_day = market_day
    if current:
        segments.append(current)
    return segments


def _segmented_autocorrelation(
    return_segments: Sequence[Sequence[float]],
) -> float:
    pairs = [
        (left, right)
        for segment in return_segments
        for left, right in zip(segment, segment[1:])
    ]
    if len(pairs) < 2:
        return 0.0
    left = [pair[0] for pair in pairs]
    right = [pair[1] for pair in pairs]
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean)
        for x, y in pairs
    )
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    )
    return numerator / denominator if denominator > 0 else 0.0


def _max_drawdown(values: Sequence[float]) -> float:
    peak = 0.0
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, (peak - value) / peak)
    return drawdown


def _quote_spread_bps(quote: Quote | None) -> float | None:
    if quote is None:
        return None
    bid = _finite_positive(quote.bid)
    ask = _finite_positive(quote.ask)
    if bid is None or ask is None or ask < bid:
        return None
    midpoint = (bid + ask) / 2
    return (ask - bid) / midpoint * 10_000 if midpoint > 0 else None


def build_watchlist_quant_metrics(
    *,
    symbol: str,
    market: str,
    daily: Sequence[DailyBar],
    intraday: Sequence[DailyBar],
    quote: Quote | None,
) -> WatchlistQuantMetrics:
    daily = sorted(daily, key=lambda candle: candle.timestamp)
    intraday = sorted(intraday, key=lambda candle: candle.timestamp)
    daily_closes = [
        value
        for candle in daily
        if (value := _finite_positive(candle.close)) is not None
    ]
    intraday_close_segments = _intraday_close_segments(
        intraday,
        market=market,
    )
    intraday_closes = [
        value
        for segment in intraday_close_segments
        for value in segment
    ]
    last_price = (
        _finite_positive(quote.last_price)
        if quote is not None
        else None
    ) or (daily_closes[-1] if daily_closes else 0.0)

    daily_returns = _log_returns(daily_closes)
    intraday_return_segments = [
        _log_returns(segment)
        for segment in intraday_close_segments
    ]
    intraday_returns = [
        value
        for segment in intraday_return_segments
        for value in segment
    ]

    dollar_volumes = [
        close * volume
        for candle in daily[-20:]
        if (close := _finite_positive(candle.close)) is not None
        and (volume := _finite_positive(candle.volume)) is not None
    ]
    median_daily_dollar_volume = (
        statistics.median(dollar_volumes) if dollar_volumes else 0.0
    )

    true_ranges: list[float] = []
    valid_daily = [
        candle
        for candle in daily
        if _finite_positive(candle.close) is not None
        and _finite_positive(candle.high) is not None
        and _finite_positive(candle.low) is not None
    ]
    for previous, current in zip(valid_daily[-15:-1], valid_daily[-14:]):
        previous_close = float(previous.close)
        high = float(current.high)
        low = float(current.low)
        true_ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
    atr_pct = (
        statistics.fmean(true_ranges) / last_price * 100
        if true_ranges and last_price > 0
        else 0.0
    )

    annualized_volatility_pct = (
        statistics.stdev(daily_returns[-20:]) * math.sqrt(252) * 100
        if len(daily_returns) >= 20
        else 0.0
    )
    return_20d_pct = (
        (daily_closes[-1] / daily_closes[-21] - 1) * 100
        if len(daily_closes) >= 21
        else 0.0
    )
    drawdown_60d_pct = _max_drawdown(daily_closes[-60:]) * 100

    absolute_intraday_returns = [abs(value) for value in intraday_returns]
    shock_threshold = (
        statistics.median(absolute_intraday_returns)
        if absolute_intraday_returns
        else 0.0
    )
    shock_pairs = [
        (current, following)
        for segment in intraday_return_segments
        for current, following in zip(segment, segment[1:])
        if shock_threshold > 0 and abs(current) >= shock_threshold
    ]
    intraday_reversal_rate = (
        sum(1 for current, following in shock_pairs if current * following < 0)
        / len(shock_pairs)
        if shock_pairs
        else 0.0
    )
    total_path = sum(absolute_intraday_returns)
    intraday_efficiency = (
        sum(abs(sum(segment)) for segment in intraday_return_segments)
        / total_path
        if total_path > 0
        else 1.0
    )
    horizon_moves = [
        abs(math.log(segment[index] / segment[index - 6])) * 10_000
        for segment in intraday_close_segments
        for index in range(6, len(segment))
        if segment[index - 6] > 0
    ]

    spread_bps = _quote_spread_bps(quote)
    blockers: list[str] = []
    if len(daily_closes) < _MIN_DAILY_BARS:
        blockers.append("INSUFFICIENT_DAILY_DATA")
    if len(intraday_closes) < _MIN_INTRADAY_BARS:
        blockers.append("INSUFFICIENT_INTRADAY_DATA")
    if last_price <= 0:
        blockers.append("INVALID_PRICE")
    if median_daily_dollar_volume < _MIN_DOLLAR_VOLUME:
        blockers.append("LOW_DOLLAR_VOLUME")
    if spread_bps is None:
        blockers.append("MISSING_BBO")
    elif spread_bps > 12:
        blockers.append("WIDE_SPREAD")
    if return_20d_pct < -15:
        blockers.append("SEVERE_DOWNTREND")
    if return_20d_pct > 30:
        blockers.append("OVERHEATED_TREND")
    if drawdown_60d_pct > 40:
        blockers.append("EXCESSIVE_DRAWDOWN")
    if annualized_volatility_pct > 130 or atr_pct > 12:
        blockers.append("EXCESSIVE_VOLATILITY")

    return WatchlistQuantMetrics(
        symbol=symbol,
        market=market,
        daily_bars=len(daily_closes),
        intraday_bars=len(intraday_closes),
        last_price=last_price,
        median_daily_dollar_volume=median_daily_dollar_volume,
        spread_bps=spread_bps,
        atr_pct=atr_pct,
        annualized_volatility_pct=annualized_volatility_pct,
        return_20d_pct=return_20d_pct,
        drawdown_60d_pct=drawdown_60d_pct,
        intraday_autocorrelation=_segmented_autocorrelation(
            intraday_return_segments,
        ),
        intraday_reversal_rate=intraday_reversal_rate,
        intraday_efficiency=intraday_efficiency,
        horizon_move_p75_bps=_percentile(horizon_moves, 0.75),
        blockers=tuple(blockers),
    )


def score_watchlist_quant_metrics(
    metrics: WatchlistQuantMetrics,
) -> WatchlistQuantScore:
    dollar_volume = max(metrics.median_daily_dollar_volume, 1.0)
    liquidity = _clamp((math.log10(dollar_volume) - 8.0) / 2.0)
    spread = _clamp(
        1.0 - (metrics.spread_bps if metrics.spread_bps is not None else 12.0) / 8.0
    )
    data_completeness = min(
        1.0,
        metrics.daily_bars / 90,
        metrics.intraday_bars / 700,
    )
    atr_fit = _clamp(1.0 - abs(metrics.atr_pct - 3.0) / 3.0)
    volatility_fit = _clamp(
        1.0 - abs(metrics.annualized_volatility_pct - 50.0) / 60.0
    )
    volatility = atr_fit * 0.65 + volatility_fit * 0.35

    trend = _clamp((metrics.return_20d_pct + 5.0) / 18.0)
    trend *= _clamp(
        1.0 - max(0.0, metrics.return_20d_pct - 25.0) / 20.0
    )

    autocorrelation = _clamp(
        (0.08 - metrics.intraday_autocorrelation) / 0.25
    )
    reversal = _clamp(
        (metrics.intraday_reversal_rate - 0.45) / 0.16
    )
    low_efficiency = _clamp(
        (0.20 - metrics.intraday_efficiency) / 0.20
    )
    mean_reversion = (
        autocorrelation * 0.35
        + reversal * 0.45
        + low_efficiency * 0.20
    )

    estimated_fee_bps = 60.0 if metrics.market.upper() == "HK" else 10.0
    estimated_cost_bps = (
        estimated_fee_bps
        + (metrics.spread_bps if metrics.spread_bps is not None else 12.0)
        + (5.0 if metrics.market.upper() == "HK" else 2.0)
    )
    edge_to_cost = (
        metrics.horizon_move_p75_bps / estimated_cost_bps
        if estimated_cost_bps > 0
        else 0.0
    )
    tradable_edge = _clamp((edge_to_cost - 0.8) / 1.7)
    drawdown = _clamp(1.0 - metrics.drawdown_60d_pct / 35.0)

    score = (
        liquidity * 15
        + spread * 15
        + data_completeness * 5
        + volatility * 15
        + trend * 15
        + mean_reversion * 20
        + tradable_edge * 10
        + drawdown * 5
    )
    if metrics.blockers:
        score = min(score, 39.0)
    score = round(_clamp(score, 0.0, 100.0), 2)

    if metrics.blockers:
        recommended_action = "AVOID"
    elif score >= 50:
        recommended_action = "CANDIDATE"
    elif score >= 40:
        recommended_action = "WATCH"
    else:
        recommended_action = "AVOID"

    confidence = round(
        _clamp(0.55 + data_completeness * 0.40 - len(metrics.blockers) * 0.10),
        4,
    )
    spread_label = (
        f"{metrics.spread_bps:.2f}bp"
        if metrics.spread_bps is not None
        else "missing"
    )
    blocker_label = (
        f"; blockers={','.join(metrics.blockers)}"
        if metrics.blockers
        else ""
    )
    rationale = (
        "quant-v1"
        f"; dollar_volume={metrics.median_daily_dollar_volume / 1_000_000:.1f}m"
        f"; spread={spread_label}"
        f"; atr={metrics.atr_pct:.2f}%"
        f"; return20={metrics.return_20d_pct:+.2f}%"
        f"; reversal5m={metrics.intraday_reversal_rate * 100:.1f}%"
        f"; autocorr5m={metrics.intraday_autocorrelation:+.3f}"
        f"; move30m_p75={metrics.horizon_move_p75_bps:.1f}bp"
        f"; drawdown60={metrics.drawdown_60d_pct:.2f}%"
        f"{blocker_label}"
    )
    return WatchlistQuantScore(
        score=score,
        confidence=confidence,
        recommended_action=recommended_action,
        rationale=rationale,
    )


class WatchlistQuantService:
    def __init__(
        self,
        db: Session,
        broker: WatchlistMarketDataProvider,
        *,
        now: datetime | None = None,
    ) -> None:
        self.db = db
        self.broker = broker
        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        self.now = observed_at.astimezone(timezone.utc)

    def score_items(
        self,
        items: Sequence[WatchlistItem],
        *,
        ttl_minutes: int = 360,
    ) -> list[WatchlistScore]:
        if not items:
            return []
        symbols = [item.symbol for item in items]
        quotes = {
            quote.symbol: quote
            for quote in self.broker.get_quotes(symbols)
        }
        score_service = WatchlistScoreService(self.db)
        rows: list[WatchlistScore] = []
        for item in items:
            try:
                daily = completed_daily_bars(
                    self.broker.get_candlesticks(
                        item.symbol,
                        "DAY",
                        _DAILY_COUNT,
                    ),
                    market=item.market,
                    now=self.now,
                )
                intraday = self._completed_intraday_bars(
                    self.broker.get_candlesticks(
                        item.symbol,
                        "MIN_5",
                        _INTRADAY_COUNT,
                    )
                )
                metrics = build_watchlist_quant_metrics(
                    symbol=item.symbol,
                    market=item.market,
                    daily=daily,
                    intraday=intraday,
                    quote=quotes.get(item.symbol),
                )
                result = score_watchlist_quant_metrics(metrics)
                row = score_service.record_score(
                    symbol=item.symbol,
                    market=item.market,
                    score=result.score,
                    rationale=result.rationale,
                    confidence=result.confidence,
                    recommended_action=result.recommended_action,
                    source="quant_v1",
                    ttl_minutes=ttl_minutes,
                    commit=False,
                )
            except Exception as exc:
                logger.warning(
                    "quant watchlist scoring failed for %s: %s",
                    item.symbol,
                    exc,
                    exc_info=True,
                )
                row = score_service.record_score(
                    symbol=item.symbol,
                    market=item.market,
                    score=0.0,
                    rationale=f"quant-v1 data error: {type(exc).__name__}",
                    confidence=0.0,
                    recommended_action="AVOID",
                    source="quant_error",
                    ttl_minutes=ttl_minutes,
                    commit=False,
                )
            rows.append(row)
        score_service.prune_history(now=self.now, commit=False)
        self.db.commit()
        for row in rows:
            self.db.refresh(row)
        rows.sort(key=lambda row: (-float(row.score), row.symbol))
        return rows

    def _completed_intraday_bars(
        self,
        bars: Sequence[BrokerCandle],
    ) -> list[BrokerCandle]:
        duration = timedelta(minutes=5)
        completed: list[BrokerCandle] = []
        for bar in bars:
            timestamp = bar.timestamp
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            else:
                timestamp = timestamp.astimezone(timezone.utc)
            if timestamp + duration <= self.now:
                completed.append(bar)
        return sorted(completed, key=lambda bar: bar.timestamp)
