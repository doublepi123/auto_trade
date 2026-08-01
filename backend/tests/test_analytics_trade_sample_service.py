from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, OrderRecord, RuntimeStateSnapshot, StrategyConfig
from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    market_local_datetime,
    mixed_currency_error,
    trade_local_day,
)
from app.services.daily_pnl_service import ClosedRoundTrip, RoundTripReplayResult


def _order(
    *,
    broker_order_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    filled_at: datetime,
) -> OrderRecord:
    return OrderRecord(
        broker_order_id=broker_order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        executed_quantity=quantity,
        price=price,
        executed_price=price,
        status="FILLED",
        created_at=filled_at,
        filled_at=filled_at,
    )


def test_loader_replays_pre_window_entry_with_current_fees_and_stable_order() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc)
    with Session(engine) as db:
        db.add(StrategyConfig(fee_rate_us=0.001, fee_rate_hk=0.004))
        db.add_all([
            _order(
                broker_order_id="buy-old",
                symbol="AAPL.US",
                side="BUY",
                quantity=2,
                price=100,
                filled_at=now - timedelta(days=40),
            ),
            _order(
                broker_order_id="sell-2",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=112,
                filled_at=now - timedelta(days=2),
            ),
            _order(
                broker_order_id="sell-1",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=110,
                filled_at=now - timedelta(days=2),
            ),
        ])
        db.commit()

        sample = load_analytics_trade_sample(
            db,
            lookback_days=7,
            now=now,
        )

    assert [trade.exit_broker_order_id for trade in sample.trades] == [
        "sell-2",
        "sell-1",
    ]
    assert sample.trades[0].entry_at == now - timedelta(days=40)
    assert sample.trades[0].est_fees == 0.212
    assert sample.currency == "USD"
    assert sample.quality.status == "COMPLETE"


def test_loader_excludes_market_day_with_unmatched_exit_and_reports_quality() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)
    with Session(engine) as db:
        db.add_all([
            _order(
                broker_order_id="buy",
                symbol="MSFT.US",
                side="BUY",
                quantity=1,
                price=100,
                filled_at=now - timedelta(hours=3),
            ),
            _order(
                broker_order_id="sell-good",
                symbol="MSFT.US",
                side="SELL",
                quantity=1,
                price=101,
                filled_at=now - timedelta(hours=2),
            ),
            _order(
                broker_order_id="sell-unmatched",
                symbol="MSFT.US",
                side="SELL",
                quantity=1,
                price=102,
                filled_at=now - timedelta(hours=1),
            ),
        ])
        db.commit()

        sample = load_analytics_trade_sample(db, lookback_days=7, now=now)

    assert sample.trades == []
    response = analytics_response(sample, {"sample_size": 0})
    assert response["statistics_quality"]["status"] == "UNRESOLVED"
    assert response["statistics_quality"]["omitted_day_count"] == 1


def test_loader_fails_closed_for_non_finite_raw_fill_and_quality_is_json_safe() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)
    with Session(engine) as db:
        invalid_entry = _order(
            broker_order_id="buy-infinite-quantity",
            symbol="AAPL.US",
            side="BUY",
            quantity=1,
            price=100,
            filled_at=now - timedelta(hours=2),
        )
        invalid_entry.executed_quantity = float("inf")
        db.add_all([
            invalid_entry,
            _order(
                broker_order_id="sell-after-invalid-entry",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=101,
                filled_at=now - timedelta(hours=1),
            ),
        ])
        db.commit()

        sample = load_analytics_trade_sample(db, lookback_days=7, now=now)

    assert sample.trades == []
    assert sample.quality.status == "UNRESOLVED"
    assert {
        item.issue_code for item in sample.quality.items
    } == {"INVALID_FILL_EVIDENCE", "FULL_UNMATCHED_EXIT"}
    for item in sample.quality.items:
        assert math.isfinite(item.filled_quantity)
        assert math.isfinite(item.matched_quantity)
        assert math.isfinite(item.unmatched_quantity)
    response = analytics_response(sample, {"sample_size": 0})
    json.dumps(response, allow_nan=False)


def test_mixed_currency_error_prevents_unconverted_pnl_total() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc)
    with Session(engine) as db:
        for symbol, prefix in (("AAPL.US", "us"), ("0700.HK", "hk")):
            db.add_all([
                _order(
                    broker_order_id=f"{prefix}-buy",
                    symbol=symbol,
                    side="BUY",
                    quantity=1,
                    price=100,
                    filled_at=now - timedelta(days=2, hours=1),
                ),
                _order(
                    broker_order_id=f"{prefix}-sell",
                    symbol=symbol,
                    side="SELL",
                    quantity=1,
                    price=101,
                    filled_at=now - timedelta(days=2),
                ),
            ])
        db.commit()

        sample = load_analytics_trade_sample(db, lookback_days=7, now=now)

    error = mixed_currency_error(sample, symbol=None, lookback_days=7)
    assert error is not None
    assert error["currency"] == "MIXED"
    assert error["totals_comparable"] is False
    assert "Mixed USD/HKD" in error["error"]


def test_market_local_helpers_use_symbol_exchange_timezone() -> None:
    instant = datetime(2026, 7, 31, 0, 30, tzinfo=timezone.utc)
    assert market_local_datetime("AAPL.US", instant).isoformat().startswith(
        "2026-07-30T20:30"
    )
    assert trade_local_day("AAPL.US", instant).isoformat() == "2026-07-30"
    assert market_local_datetime("0700.HK", instant).isoformat().startswith(
        "2026-07-31T08:30"
    )
    assert trade_local_day("0700.HK", instant).isoformat() == "2026-07-31"


def test_loader_fails_closed_for_non_finite_or_non_causal_trade_evidence(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc)
    invalid = ClosedRoundTrip(
        symbol="AAPL.US",
        side="long",
        entry_order_id=1,
        exit_order_id=2,
        entry_at=now,
        exit_at=now - timedelta(minutes=1),
        entry_price=100,
        exit_price=101,
        quantity=1,
        gross_pnl=1,
        est_fees=0.1,
        net_pnl=float("nan"),
        holding_seconds=-60,
        exit_broker_order_id="invalid-exit",
    )
    valid_same_day = ClosedRoundTrip(
        symbol="AAPL.US",
        side="long",
        entry_order_id=3,
        exit_order_id=4,
        entry_at=now - timedelta(hours=2),
        exit_at=now - timedelta(hours=1),
        entry_price=100,
        exit_price=101,
        quantity=1,
        gross_pnl=1,
        est_fees=0.1,
        net_pnl=0.9,
        holding_seconds=3600,
        exit_broker_order_id="valid-exit",
    )
    monkeypatch.setattr(
        "app.services.analytics_trade_sample_service.DailyPnlService."
        "pair_round_trips_with_issues",
        lambda self, **kwargs: RoundTripReplayResult(
            trades=[invalid, valid_same_day],
            issues=[],
        ),
    )

    with Session(engine) as db:
        sample = load_analytics_trade_sample(db, lookback_days=7, now=now)

    assert sample.trades == []
    assert sample.quality.status == "UNRESOLVED"
    assert sample.quality.omitted_day_count == 1
    assert sample.quality.items[0].issue_code == "INVALID_CLOSED_TRADE_EVIDENCE"


def test_excursion_enrichment_does_not_invent_path_from_entry_and_exit_only() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc)
    entry_at = now - timedelta(hours=2)
    exit_at = now - timedelta(hours=1)
    with Session(engine) as db:
        db.add_all([
            _order(
                broker_order_id="buy-no-path",
                symbol="AAPL.US",
                side="BUY",
                quantity=1,
                price=100,
                filled_at=entry_at,
            ),
            _order(
                broker_order_id="sell-no-path",
                symbol="AAPL.US",
                side="SELL",
                quantity=1,
                price=110,
                filled_at=exit_at,
            ),
            RuntimeStateSnapshot(
                symbol="AAPL.US",
                last_price=100,
                created_at=entry_at,
            ),
            RuntimeStateSnapshot(
                symbol="AAPL.US",
                last_price=110,
                created_at=exit_at,
            ),
        ])
        db.commit()

        sample = load_analytics_trade_sample(
            db,
            lookback_days=7,
            now=now,
            include_excursions=True,
        )

    assert len(sample.trades) == 1
    assert sample.trades[0].mfe_amount is None
    assert sample.trades[0].mae_amount is None
    assert sample.trades[0].mfe_pct is None
    assert sample.trades[0].mae_pct is None
    assert sample.trades[0].excursion_source == "ENDPOINT_ONLY"
    assert sample.trades[0].excursion_interior_observation_count == 0


def test_persisted_excursion_without_retained_path_is_legacy_unknown() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc)
    with Session(engine) as db:
        entry = _order(
            broker_order_id="legacy-excursion-buy",
            symbol="AAPL.US",
            side="BUY",
            quantity=1,
            price=100,
            filled_at=now - timedelta(hours=2),
        )
        exit_order = _order(
            broker_order_id="legacy-excursion-sell",
            symbol="AAPL.US",
            side="SELL",
            quantity=1,
            price=110,
            filled_at=now - timedelta(hours=1),
        )
        exit_order.mfe_amount = 10
        exit_order.mae_amount = 0
        exit_order.mfe_pct = 10
        exit_order.mae_pct = 0
        db.add_all([entry, exit_order])
        db.commit()

        sample = load_analytics_trade_sample(
            db,
            lookback_days=7,
            now=now,
            include_excursions=True,
        )

    assert len(sample.trades) == 1
    assert sample.trades[0].mfe_amount == 10
    assert sample.trades[0].excursion_source == "LEGACY_UNKNOWN"
    assert sample.trades[0].excursion_interior_observation_count == 0


def test_invalid_trade_before_utc_cutoff_taints_same_market_local_day(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc)
    cutoff = now - timedelta(days=7)
    invalid_exit = cutoff - timedelta(minutes=1)
    valid_exit = cutoff + timedelta(hours=1)
    invalid = ClosedRoundTrip(
        symbol="AAPL.US",
        side="long",
        entry_order_id=1,
        exit_order_id=2,
        entry_at=invalid_exit + timedelta(minutes=1),
        exit_at=invalid_exit,
        entry_price=100,
        exit_price=101,
        quantity=1,
        gross_pnl=1,
        est_fees=0.1,
        net_pnl=0.9,
        holding_seconds=-60,
        exit_broker_order_id="invalid-before-cutoff",
    )
    valid = ClosedRoundTrip(
        symbol="AAPL.US",
        side="long",
        entry_order_id=3,
        exit_order_id=4,
        entry_at=valid_exit - timedelta(hours=1),
        exit_at=valid_exit,
        entry_price=100,
        exit_price=101,
        quantity=1,
        gross_pnl=1,
        est_fees=0.1,
        net_pnl=0.9,
        holding_seconds=3600,
        exit_broker_order_id="valid-after-cutoff",
    )
    monkeypatch.setattr(
        "app.services.analytics_trade_sample_service.DailyPnlService."
        "pair_round_trips_with_issues",
        lambda self, **kwargs: RoundTripReplayResult(
            trades=[invalid, valid],
            issues=[],
        ),
    )

    with Session(engine) as db:
        sample = load_analytics_trade_sample(db, lookback_days=7, now=now)

    assert sample.trades == []
    assert sample.quality.status == "UNRESOLVED"
    assert sample.quality.omitted_day_count == 1
    assert sample.quality.items[0].broker_order_id == "invalid-before-cutoff"
