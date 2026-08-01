from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any

from sqlalchemy.orm import Session

from app.core.market_calendar import get_session, market_for_symbol, trade_day_for
from app.models import StrategyConfig
from app.services.daily_pnl_service import ClosedRoundTrip, DailyPnlService
from app.services.statistics_quality_service import (
    StatisticsQualityData,
    StatisticsQualityItemData,
    select_statistics_sample,
)

__all__ = [
    "AnalyticsTradeSample",
    "analytics_response",
    "currency_for_symbol",
    "load_analytics_trade_sample",
    "market_local_datetime",
    "mixed_currency_error",
    "trade_local_day",
]


@dataclass(frozen=True)
class AnalyticsTradeSample:
    """Quality-gated FIFO round trips for one analytics request window."""

    trades: list[ClosedRoundTrip]
    quality: StatisticsQualityData
    from_dt: datetime
    to_dt: datetime
    currencies: tuple[str, ...]

    @property
    def currency(self) -> str | None:
        if len(self.currencies) == 1:
            return self.currencies[0]
        if len(self.currencies) > 1:
            return "MIXED"
        return None

    @property
    def totals_comparable(self) -> bool:
        return len(self.currencies) <= 1


def load_analytics_trade_sample(
    db: Session,
    *,
    symbol: str | None = None,
    lookback_days: int,
    now: datetime | None = None,
    include_excursions: bool = False,
) -> AnalyticsTradeSample:
    """Load canonical closed trades without truncating pre-window entries.

    The ledger replay intentionally has no lower time bound. A position can be
    opened before the requested analytics window and close inside it; loading
    only recent order rows would turn that valid close into an unmatched exit.
    ``select_statistics_sample`` applies the requested exit window afterwards
    and fails closed for every market-local trade day affected by replay issues.
    """

    resolved_now = _as_utc(now or datetime.now(timezone.utc))
    from_dt = resolved_now - timedelta(days=max(1, int(lookback_days)))
    normalized_symbol = symbol.strip().upper() if symbol and symbol.strip() else None
    fee_rate_us, fee_rate_hk = _active_fee_rates(db)
    replay = DailyPnlService(db).pair_round_trips_with_issues(
        symbol=normalized_symbol,
        to_dt=resolved_now,
        fee_rate_us=fee_rate_us,
        fee_rate_hk=fee_rate_hk,
        include_excursions=include_excursions,
    )
    selected = select_statistics_sample(
        replay,
        from_dt=from_dt,
        to_dt=resolved_now,
    )
    window_trade_days = {
        (
            trade.symbol,
            trade_local_day(trade.symbol, trade.exit_at),
        )
        for trade in selected.trades
    }
    relevant_evidence = [
        trade
        for trade in replay.trades
        if from_dt <= trade.exit_at <= resolved_now
        or (
            trade.symbol,
            trade_local_day(trade.symbol, trade.exit_at),
        )
        in window_trade_days
    ]
    trades, quality = _exclude_invalid_trade_days(
        selected.trades,
        selected.quality,
        evidence_trades=relevant_evidence,
    )
    trades = sorted(
        trades,
        key=lambda trade: (trade.exit_at, trade.exit_order_id),
    )
    currencies = tuple(sorted({currency_for_symbol(trade.symbol) for trade in trades}))
    return AnalyticsTradeSample(
        trades=trades,
        quality=quality,
        from_dt=from_dt,
        to_dt=resolved_now,
        currencies=currencies,
    )


def analytics_response(
    sample: AnalyticsTradeSample,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Attach the shared evidence contract to every response branch."""

    return {
        **payload,
        "currency": sample.currency,
        "currencies": list(sample.currencies),
        "totals_comparable": sample.totals_comparable,
        "statistics_quality": asdict(sample.quality),
    }


def mixed_currency_error(
    sample: AnalyticsTradeSample,
    *,
    symbol: str | None = None,
    lookback_days: int | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Fail closed when an analysis would add unconverted USD and HKD PnL."""

    if sample.totals_comparable:
        return None
    base = (
        dict(payload)
        if payload is not None
        else {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(sample.trades),
        }
    )
    base.update(
        {
            "error": (
                "Mixed USD/HKD samples cannot be aggregated without an FX "
                "conversion rate. Select a single symbol or market."
            ),
        }
    )
    return analytics_response(sample, base)


def currency_for_symbol(symbol: str) -> str:
    return "HKD" if market_for_symbol(symbol) == "HK" else "USD"


def market_local_datetime(symbol: str, instant: datetime) -> datetime:
    return get_session(market_for_symbol(symbol)).local(instant)


def trade_local_day(symbol: str, instant: datetime) -> date:
    return trade_day_for(market_for_symbol(symbol), instant)


def _active_fee_rates(db: Session) -> tuple[float, float]:
    config = db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
    fee_us = getattr(config, "fee_rate_us", None)
    fee_hk = getattr(config, "fee_rate_hk", None)
    return (
        float(fee_us) if fee_us is not None else 0.0005,
        float(fee_hk) if fee_hk is not None else 0.003,
    )


def _exclude_invalid_trade_days(
    trades: list[ClosedRoundTrip],
    quality: StatisticsQualityData,
    *,
    evidence_trades: list[ClosedRoundTrip] | None = None,
) -> tuple[list[ClosedRoundTrip], StatisticsQualityData]:
    validation_source = evidence_trades if evidence_trades is not None else trades
    invalid = [
        trade
        for trade in validation_source
        if not _valid_trade_evidence(trade)
    ]
    if not invalid:
        return trades, quality

    invalid_days = {
        (trade.symbol, trade_local_day(trade.symbol, trade.exit_at).isoformat())
        for trade in invalid
    }
    valid = [
        trade
        for trade in trades
        if (
            trade.symbol,
            trade_local_day(trade.symbol, trade.exit_at).isoformat(),
        )
        not in invalid_days
    ]
    invalid_items = [
        StatisticsQualityItemData(
            trade_day=trade_local_day(
                trade.symbol,
                trade.exit_at,
            ).isoformat(),
            symbol=trade.symbol,
            issue_code="INVALID_CLOSED_TRADE_EVIDENCE",
            exit_order_id=trade.exit_order_id,
            broker_order_id=trade.exit_broker_order_id,
            side=trade.side,
            filled_quantity=(
                trade.quantity if math.isfinite(trade.quantity) else 0.0
            ),
            matched_quantity=0.0,
            unmatched_quantity=(
                trade.quantity
                if math.isfinite(trade.quantity) and trade.quantity > 0
                else 0.0
            ),
            reason=(
                "closed trade has non-finite, non-positive, or non-causal "
                "evidence"
            ),
        )
        for trade in invalid
    ]
    items = sorted(
        [*quality.items, *invalid_items],
        key=lambda item: (
            item.trade_day,
            item.symbol,
            item.exit_order_id,
            item.issue_code,
        ),
    )
    omitted_days = {
        (item.symbol, item.trade_day)
        for item in items
    }
    return valid, StatisticsQualityData(
        status="UNRESOLVED",
        known_exclusion_count=quality.known_exclusion_count,
        unresolved_issue_count=(
            quality.unresolved_issue_count + len(invalid_items)
        ),
        omitted_day_count=len(omitted_days),
        items=items,
    )


def _valid_trade_evidence(trade: ClosedRoundTrip) -> bool:
    required = (
        trade.entry_price,
        trade.exit_price,
        trade.quantity,
        trade.gross_pnl,
        trade.est_fees,
        trade.net_pnl,
        trade.holding_seconds,
    )
    optional = (
        trade.actual_fees,
        trade.slippage_amount,
        trade.slippage_bps,
        trade.ack_latency_ms,
        trade.fill_latency_ms,
        trade.mfe_amount,
        trade.mae_amount,
        trade.mfe_pct,
        trade.mae_pct,
        trade.excursion_max_gap_seconds,
    )
    return (
        all(math.isfinite(value) for value in required)
        and all(value is None or math.isfinite(value) for value in optional)
        and trade.entry_price > 0
        and trade.exit_price > 0
        and trade.quantity > 0
        and trade.est_fees >= 0
        and (trade.actual_fees is None or trade.actual_fees >= 0)
        and trade.holding_seconds >= 0
        and isinstance(trade.excursion_interior_observation_count, int)
        and trade.excursion_interior_observation_count >= 0
        and trade.entry_at <= trade.exit_at
        and trade.exit_order_id >= 0
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
