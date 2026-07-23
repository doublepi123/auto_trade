from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.broker import BrokerCandle, Quote
from app.core.market_calendar import get_session
from app.models import Base, WatchlistItem, WatchlistScore
from app.services import watchlist_quant_service as quant_module
from app.services.watchlist_quant_service import (
    WatchlistQuantMetrics,
    WatchlistQuantService,
    build_watchlist_quant_metrics,
    score_watchlist_quant_metrics,
)

_NOW = datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _daily_bars() -> list[BrokerCandle]:
    bars: list[BrokerCandle] = []
    price = 100.0
    start = _NOW - timedelta(days=100)
    for index in range(90):
        move = 0.009 if index % 2 == 0 else -0.006
        close = price * (1 + move)
        bars.append(
            BrokerCandle(
                timestamp=start + timedelta(days=index),
                open=price,
                high=max(price, close) * 1.012,
                low=min(price, close) * 0.988,
                close=close,
                volume=12_000_000,
            )
        )
        price = close
    return bars


def _rth_timestamps(
    count: int,
    *,
    now: datetime = _NOW,
) -> list[datetime]:
    session = get_session("US")
    cursor = now - timedelta(minutes=5)
    timestamps: list[datetime] = []
    while len(timestamps) < count:
        if session.is_rth(cursor):
            timestamps.append(cursor)
        cursor -= timedelta(minutes=5)
    return list(reversed(timestamps))


def _intraday_bars(
    *,
    count: int = 700,
    now: datetime = _NOW,
) -> list[BrokerCandle]:
    bars: list[BrokerCandle] = []
    previous = 110.0
    for index, timestamp in enumerate(_rth_timestamps(count, now=now)):
        close = 110 * math.exp(
            0.006 * math.sin(2 * math.pi * index / 8)
        )
        bars.append(
            BrokerCandle(
                timestamp=timestamp,
                open=previous,
                high=max(previous, close) * 1.0005,
                low=min(previous, close) * 0.9995,
                close=close,
                volume=100_000,
            )
        )
        previous = close
    return bars


def _strong_metrics(
    *,
    blockers: tuple[str, ...] = (),
    spread_bps: float | None = 0.6,
) -> WatchlistQuantMetrics:
    return WatchlistQuantMetrics(
        symbol="AAPL.US",
        market="US",
        daily_bars=90,
        intraday_bars=800,
        last_price=220.0,
        median_daily_dollar_volume=5_000_000_000,
        spread_bps=spread_bps,
        atr_pct=3.0,
        annualized_volatility_pct=50.0,
        return_20d_pct=10.0,
        drawdown_60d_pct=5.0,
        intraday_autocorrelation=-0.10,
        intraday_reversal_rate=0.62,
        intraday_efficiency=0.04,
        horizon_move_p75_bps=65.0,
        conditional_reversal_bps=65.0,
        conditional_reversal_hit_rate=0.70,
        reversal_observations=250,
        latest_intraday_at=_NOW - timedelta(minutes=5),
        blockers=blockers,
    )


def test_strong_liquid_mean_reverting_symbol_is_preferred() -> None:
    result = score_watchlist_quant_metrics(_strong_metrics())

    assert result.score >= 50
    assert result.recommended_action == "CANDIDATE"
    assert result.confidence >= 0.9
    assert "reversal30m=+65.0bp" in result.rationale
    assert "net_reversal30m=+50.4bp" in result.rationale


def test_candidate_requires_positive_cost_adjusted_reversal_edge() -> None:
    result = score_watchlist_quant_metrics(
        replace(
            _strong_metrics(),
            conditional_reversal_bps=10.0,
        )
    )

    assert result.score <= 49
    assert result.recommended_action != "CANDIDATE"
    assert "net_reversal30m=-4.6bp" in result.rationale


def test_candidate_requires_configured_minimum_edge_cost_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        quant_module.settings,
        "min_entry_edge_cost_ratio",
        2.0,
    )

    result = score_watchlist_quant_metrics(
        replace(
            _strong_metrics(),
            conditional_reversal_bps=20.0,
        )
    )

    assert result.score == 49
    assert result.recommended_action != "CANDIDATE"
    assert "edge_cost_ratio=1.370x" in result.rationale
    assert "required_edge_cost_ratio=2.000x" in result.rationale


def test_hard_blocker_caps_score_and_forces_avoid() -> None:
    result = score_watchlist_quant_metrics(
        _strong_metrics(
            blockers=("WIDE_SPREAD",),
            spread_bps=25.0,
        )
    )

    assert result.score <= 39
    assert result.recommended_action == "AVOID"
    assert "blockers=WIDE_SPREAD" in result.rationale


class _FakeBroker:
    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        return [
            Quote(
                symbol=symbol,
                last_price=110.0,
                bid=109.99,
                ask=110.01,
                timestamp=_NOW.isoformat(),
            )
            for symbol in symbols
        ]

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        if symbol == "BROKEN.US" and period == "MIN_5":
            raise RuntimeError("intraday unavailable")
        if period == "DAY":
            return _daily_bars()[-count:]
        return _intraday_bars()[-count:]


def test_service_persists_sorted_scores_and_isolates_symbol_failure() -> None:
    db = _db()
    try:
        items = [
            WatchlistItem(
                symbol="AAPL.US",
                market="US",
                alias="Apple",
            ),
            WatchlistItem(
                symbol="BROKEN.US",
                market="US",
                alias="Broken",
            ),
        ]
        db.add_all(items)
        db.commit()

        rows = WatchlistQuantService(
            db,
            _FakeBroker(),
            now=_NOW,
        ).score_items(items, ttl_minutes=120)

        assert [row.symbol for row in rows] == [
            "AAPL.US",
            "BROKEN.US",
        ]
        assert rows[0].source == "quant_v2"
        assert rows[1].source == "quant_error_v2"
        assert rows[1].score == 0
        assert rows[1].recommended_action == "AVOID"
        assert db.query(WatchlistScore).count() == 2
    finally:
        db.close()


def test_incomplete_current_intraday_bar_is_excluded() -> None:
    db = _db()
    try:
        service = WatchlistQuantService(db, _FakeBroker(), now=_NOW)
        completed = BrokerCandle(
            timestamp=_NOW - timedelta(minutes=5),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1,
        )
        current = BrokerCandle(
            timestamp=_NOW - timedelta(minutes=2),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1,
        )

        assert service._completed_intraday_bars([current, completed]) == [
            completed
        ]
    finally:
        db.close()


def test_stale_intraday_data_blocks_even_when_last_trade_is_old() -> None:
    metrics = build_watchlist_quant_metrics(
        symbol="AAPL.US",
        market="US",
        daily=_daily_bars(),
        intraday=_intraday_bars(now=_NOW - timedelta(minutes=30)),
        quote=Quote(
            symbol="AAPL.US",
            last_price=110,
            bid=109.99,
            ask=110.01,
            timestamp=(_NOW - timedelta(minutes=2)).isoformat(),
        ),
        observed_at=_NOW,
    )
    result = score_watchlist_quant_metrics(metrics)

    assert "STALE_INTRADAY_DATA" in metrics.blockers
    assert "STALE_BBO" not in metrics.blockers
    assert result.score <= 39
    assert result.recommended_action == "AVOID"


def test_extended_hours_bars_do_not_change_rth_edge_metrics() -> None:
    regular = _intraday_bars()
    quote = Quote(
        symbol="AAPL.US",
        last_price=110,
        bid=109.99,
        ask=110.01,
        timestamp=_NOW.isoformat(),
    )
    baseline = build_watchlist_quant_metrics(
        symbol="AAPL.US",
        market="US",
        daily=_daily_bars(),
        intraday=regular,
        quote=quote,
        observed_at=_NOW,
    )
    extended = [
        BrokerCandle(
            timestamp=datetime(
                2026,
                7,
                24,
                12,
                0,
                tzinfo=timezone.utc,
            ),
            open=110,
            high=220,
            low=55,
            close=220,
            volume=1,
        ),
        *regular,
        BrokerCandle(
            timestamp=datetime(
                2026,
                7,
                23,
                21,
                0,
                tzinfo=timezone.utc,
            ),
            open=110,
            high=220,
            low=55,
            close=55,
            volume=1,
        ),
    ]
    observed = build_watchlist_quant_metrics(
        symbol="AAPL.US",
        market="US",
        daily=_daily_bars(),
        intraday=extended,
        quote=quote,
        observed_at=_NOW,
    )

    assert observed.intraday_bars == baseline.intraday_bars
    assert (
        observed.conditional_reversal_bps
        == baseline.conditional_reversal_bps
    )
    assert (
        observed.conditional_reversal_hit_rate
        == baseline.conditional_reversal_hit_rate
    )
    assert observed.reversal_observations == baseline.reversal_observations


def test_overnight_gap_does_not_inflate_intraday_edge_metrics() -> None:
    intraday: list[BrokerCandle] = []
    for start, price in (
        (datetime(2026, 7, 22, 14, 30, tzinfo=timezone.utc), 100.0),
        (datetime(2026, 7, 23, 14, 30, tzinfo=timezone.utc), 200.0),
    ):
        for index in range(7):
            intraday.append(
                BrokerCandle(
                    timestamp=start + timedelta(minutes=5 * index),
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=100_000,
                )
            )

    metrics = build_watchlist_quant_metrics(
        symbol="AAPL.US",
        market="US",
        daily=_daily_bars(),
        intraday=intraday,
        quote=Quote(
            symbol="AAPL.US",
            last_price=200,
            bid=199.99,
            ask=200.01,
            timestamp=_NOW.isoformat(),
        ),
    )

    assert metrics.horizon_move_p75_bps == 0
    assert metrics.intraday_autocorrelation == 0
    assert metrics.intraday_reversal_rate == 0
