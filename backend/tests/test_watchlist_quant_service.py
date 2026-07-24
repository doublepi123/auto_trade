from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api import watchlist as watchlist_api
from app.core.broker import BrokerCandle, Quote
from app.core.market_calendar import get_session
from app.models import Base, StrategyConfig, WatchlistItem, WatchlistScore
from app.services import watchlist_quant_service as quant_module
from app.services.watchlist_quant_service import (
    QuantScoringOutsideRTHError,
    WatchlistQuantService,
    build_watchlist_quant_metrics,
    score_watchlist_quant_metrics,
)

_NOW = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)
_US_ONE_SIDE_FEE_RATE = 0.0005


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _daily_bars() -> list[BrokerCandle]:
    result: list[BrokerCandle] = []
    price = 100.0
    end = datetime(2026, 7, 22, 4, tzinfo=timezone.utc)
    for index in range(90):
        close = price * (1.01 if index % 2 == 0 else 0.99)
        result.append(
            BrokerCandle(
                timestamp=end - timedelta(days=89 - index),
                open=price,
                high=max(price, close) * 1.007,
                low=min(price, close) * 0.993,
                close=close,
                volume=12_000_000,
            )
        )
        price = close
    return result


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
    count: int = 1000,
    now: datetime = _NOW,
) -> list[BrokerCandle]:
    result: list[BrokerCandle] = []
    previous = 100.0
    previous_timestamp: datetime | None = None
    segment_index = -1
    bar_index = 0
    for timestamp in _rth_timestamps(count, now=now):
        if (
            previous_timestamp is None
            or timestamp - previous_timestamp > timedelta(minutes=7)
        ):
            segment_index += 1
            bar_index = 0
        else:
            bar_index += 1
        if segment_index < 2:
            log_return = 0.003 * (-1 if bar_index % 2 else 1)
        elif bar_index % 8 == 1:
            log_return = -0.01
        elif 2 <= bar_index % 8 <= 7:
            log_return = 0.001
        else:
            log_return = 0.0
        close = previous * math.exp(log_return)
        result.append(
            BrokerCandle(
                timestamp=timestamp,
                open=previous,
                high=max(previous, close) * 1.0002,
                low=min(previous, close) * 0.9998,
                close=close,
                volume=100_000,
            )
        )
        previous = close
        previous_timestamp = timestamp
    return result


def _quote(symbol: str = "AAPL.US") -> Quote:
    return Quote(
        symbol=symbol,
        last_price=100,
        bid=99.99,
        ask=100.01,
        timestamp=_NOW.isoformat(),
    )


def test_quant_score_rewards_liquid_mean_reverting_candidate() -> None:
    metrics = build_watchlist_quant_metrics(
        symbol="AAPL.US",
        market="US",
        daily=_daily_bars(),
        intraday=_intraday_bars(),
        quote=_quote(),
        observed_at=_NOW,
    )

    score = score_watchlist_quant_metrics(
        metrics,
        estimated_one_side_fee_rate=_US_ONE_SIDE_FEE_RATE,
    )

    assert metrics.blockers == ()
    assert metrics.conditional_reversal_bps > 40
    assert metrics.conditional_reversal_hit_rate > 0.9
    assert score.score >= 50
    assert score.recommended_action == "CANDIDATE"
    assert score.rationale.startswith("quant-v4;")


def test_quant_score_caps_candidate_with_hard_data_blockers() -> None:
    metrics = build_watchlist_quant_metrics(
        symbol="THIN.US",
        market="US",
        daily=_daily_bars()[:10],
        intraday=_intraday_bars()[:20],
        quote=None,
        observed_at=_NOW,
    )

    score = score_watchlist_quant_metrics(
        metrics,
        estimated_one_side_fee_rate=_US_ONE_SIDE_FEE_RATE,
    )

    assert "INSUFFICIENT_DAILY_DATA" in metrics.blockers
    assert "INSUFFICIENT_INTRADAY_DATA" in metrics.blockers
    assert "MISSING_BBO" in metrics.blockers
    assert score.score <= 39
    assert score.recommended_action == "AVOID"


class _Broker:
    def __init__(self, *, fail_symbol: str = "") -> None:
        self.fail_symbol = fail_symbol
        self.quote_requests: list[list[str]] = []

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        self.quote_requests.append(list(symbols))
        return [_quote(symbol) for symbol in symbols]

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        if symbol == self.fail_symbol:
            raise RuntimeError("market data failed")
        if period == "DAY":
            return _daily_bars()
        values = _intraday_bars()
        values.append(
            BrokerCandle(
                timestamp=_NOW,
                open=100,
                high=101,
                low=99,
                close=100,
                volume=100,
            )
        )
        return values


class _PagedBroker(_Broker):
    def __init__(self) -> None:
        super().__init__()
        self.intraday = _intraday_bars(count=3000)
        self.history_requests: list[
            tuple[str, str, int, datetime]
        ] = []

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        if period == "DAY":
            return _daily_bars()
        return self.intraday[-count:]

    def get_history_candlesticks_before(
        self,
        symbol: str,
        period: str,
        count: int,
        before: datetime,
    ) -> list[BrokerCandle]:
        self.history_requests.append(
            (symbol, period, count, before)
        )
        eligible = [
            bar
            for bar in self.intraday
            if bar.timestamp < before
        ]
        return eligible[-count:]


class _InvalidHistoryBroker(_Broker):
    def get_history_candlesticks_before(
        self,
        symbol: str,
        period: str,
        count: int,
        before: datetime,
    ) -> tuple[BrokerCandle, ...]:
        del symbol, period, count, before
        return ()


def test_service_pages_three_thousand_intraday_bars() -> None:
    db = _db()
    try:
        item = WatchlistItem(
            symbol="AAPL.US",
            market="US",
            alias="Apple",
        )
        db.add(item)
        db.commit()
        broker = _PagedBroker()

        rows = WatchlistQuantService(
            db,
            broker,
            now=_NOW,
        ).score_items([item])

        assert len(rows) == 1
        assert rows[0].source == "quant_v4"
        assert "intraday_bars=3000" in rows[0].rationale
        assert [
            (symbol, period, count)
            for symbol, period, count, _before
            in broker.history_requests
        ] == [
            ("AAPL.US", "MIN_5", 1000),
            ("AAPL.US", "MIN_5", 1000),
        ]
        assert (
            broker.history_requests[1][3]
            < broker.history_requests[0][3]
        )
    finally:
        db.close()


def test_service_rejects_malformed_historical_page() -> None:
    db = _db()
    try:
        item = WatchlistItem(
            symbol="AAPL.US",
            market="US",
            alias="Apple",
        )
        db.add(item)
        db.commit()

        rows = WatchlistQuantService(
            db,
            _InvalidHistoryBroker(),
            now=_NOW,
        ).score_items([item])

        assert len(rows) == 1
        assert rows[0].source == "quant_error_v4"
        assert rows[0].recommended_action == "AVOID"
        assert rows[0].rationale == "quant-v4 data error: ValueError"
    finally:
        db.close()


class _QuoteFailureBroker:
    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        raise RuntimeError(f"quote request failed: {symbols}")

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        raise AssertionError(
            f"unexpected candle access: {symbol} {period} {count}"
        )


def test_service_persists_scores_and_isolates_symbol_failures() -> None:
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
            _Broker(fail_symbol="BROKEN.US"),
            now=_NOW,
        ).score_items(items)

        assert [row.symbol for row in rows] == [
            "AAPL.US",
            "BROKEN.US",
        ]
        by_symbol = {row.symbol: row for row in rows}
        assert by_symbol["AAPL.US"].source == "quant_v4"
        assert by_symbol["BROKEN.US"].source == "quant_error_v4"
        assert by_symbol["BROKEN.US"].recommended_action == "AVOID"
        assert db.query(WatchlistScore).count() == 2
    finally:
        db.close()


def test_market_data_failure_does_not_commit_pending_session_state() -> None:
    db = _db()
    try:
        scored_item = WatchlistItem(
            symbol="AAPL.US",
            market="US",
            alias="Apple",
        )
        db.add(scored_item)
        db.commit()
        db.refresh(scored_item)
        pending_item = WatchlistItem(
            symbol="PENDING.US",
            market="US",
            alias="Pending",
        )
        db.add(pending_item)

        with pytest.raises(RuntimeError, match="quote request failed"):
            WatchlistQuantService(
                db,
                _QuoteFailureBroker(),
                now=_NOW,
            ).score_items([scored_item])
        assert pending_item in db.new
        db.rollback()

        assert db.query(WatchlistItem).count() == 1
        assert (
            db.query(WatchlistItem)
            .filter(WatchlistItem.symbol == "PENDING.US")
            .count()
            == 0
        )
        assert db.query(StrategyConfig).count() == 0
        assert db.query(WatchlistScore).count() == 0
    finally:
        db.close()


def test_service_uses_latest_strategy_fee_to_downgrade_candidate() -> None:
    db = _db()
    try:
        strategy = StrategyConfig(
            fee_rate_us=_US_ONE_SIDE_FEE_RATE,
            fee_rate_hk=0.003,
        )
        item = WatchlistItem(
            symbol="AAPL.US",
            market="US",
            alias="Apple",
        )
        db.add_all([strategy, item])
        db.commit()
        service = WatchlistQuantService(db, _Broker(), now=_NOW)

        baseline = service.score_items([item])[0]
        strategy.fee_rate_us = 0.005
        db.commit()
        high_fee = service.score_items([item])[0]

        assert baseline.recommended_action == "CANDIDATE"
        assert "one_side_fee=5.0bp" in baseline.rationale
        assert high_fee.score <= 49
        assert high_fee.recommended_action != "CANDIDATE"
        assert "one_side_fee=50.0bp" in high_fee.rationale
        assert "round_trip_fee=100.0bp" in high_fee.rationale
    finally:
        db.close()


class _NoMarketDataBroker:
    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        raise AssertionError(f"unexpected quote access: {symbols}")

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        raise AssertionError(
            f"unexpected candle access: {symbol} {period} {count}"
        )


def test_service_rejects_outside_rth_before_market_data_or_writes() -> None:
    db = _db()
    try:
        item = WatchlistItem(
            symbol="AAPL.US",
            market="US",
            alias="Apple",
        )
        db.add(item)
        db.commit()

        with pytest.raises(
            QuantScoringOutsideRTHError,
            match="regular trading hours.*US",
        ):
            WatchlistQuantService(
                db,
                _NoMarketDataBroker(),
                now=datetime(2026, 7, 23, 23, 0, tzinfo=timezone.utc),
            ).score_items([item])

        assert db.query(WatchlistScore).count() == 0
        assert db.query(StrategyConfig).count() == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    ("symbol", "market", "observed_at"),
    [
        (
            "AAPL.US",
            "US",
            datetime(2026, 7, 23, 13, 32, tzinfo=timezone.utc),
        ),
        (
            "700.HK",
            "HK",
            datetime(2026, 7, 24, 1, 32, tzinfo=timezone.utc),
        ),
        (
            "700.HK",
            "HK",
            datetime(2026, 7, 24, 5, 2, tzinfo=timezone.utc),
        ),
    ],
)
def test_service_preserves_score_until_first_segment_bar_completes(
    symbol: str,
    market: str,
    observed_at: datetime,
) -> None:
    db = _db()
    try:
        item = WatchlistItem(
            symbol=symbol,
            market=market,
            alias=symbol,
        )
        previous = WatchlistScore(
            symbol=symbol,
            market=market,
            score=72,
            confidence=0.8,
            recommended_action="CANDIDATE",
            source="quant_v4",
            rationale="previous valid score",
            created_at=observed_at - timedelta(hours=1),
            expires_at=observed_at + timedelta(hours=1),
        )
        db.add_all([item, previous])
        db.commit()
        previous_id = previous.id

        rows = WatchlistQuantService(
            db,
            _NoMarketDataBroker(),
            now=observed_at,
        ).score_items([item])

        assert rows == []
        stored = db.query(WatchlistScore).all()
        assert len(stored) == 1
        assert stored[0].id == previous_id
        assert stored[0].rationale == "previous valid score"
        assert db.query(StrategyConfig).count() == 0
    finally:
        db.close()


def test_quant_rank_api_maps_closed_market_to_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db()
    try:
        db.add(
            WatchlistItem(
                symbol="AAPL.US",
                market="US",
                alias="Apple",
            )
        )
        db.commit()
        monkeypatch.setattr(
            watchlist_api,
            "get_runner",
            lambda: SimpleNamespace(broker=_NoMarketDataBroker()),
        )
        monkeypatch.setattr(
            quant_module,
            "is_trading_hours",
            lambda _market, _now: False,
        )

        with pytest.raises(HTTPException) as captured:
            watchlist_api.rank_watchlist_quantitatively(
                ttl_minutes=360,
                db=db,
            )

        assert captured.value.status_code == 409
        assert "regular trading hours" in str(captured.value.detail)
        assert db.query(WatchlistScore).count() == 0
    finally:
        db.close()


def test_service_updates_open_market_and_leaves_closed_market_unchanged() -> None:
    db = _db()
    try:
        items = [
            WatchlistItem(
                symbol="AAPL.US",
                market="US",
                alias="Apple",
            ),
            WatchlistItem(
                symbol="700.HK",
                market="HK",
                alias="Tencent",
            ),
        ]
        db.add_all(items)
        db.commit()
        broker = _Broker()

        rows = WatchlistQuantService(
            db,
            broker,
            now=_NOW,
        ).score_items(items)

        assert broker.quote_requests == [["AAPL.US"]]
        assert [row.symbol for row in rows] == ["AAPL.US"]
        assert rows[0].source == "quant_v4"
        assert db.query(WatchlistScore).count() == 1
    finally:
        db.close()


def test_quant_rank_api_returns_complete_current_snapshot_for_mixed_markets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db()
    try:
        items = [
            WatchlistItem(
                symbol="AAPL.US",
                market="US",
                alias="Apple",
            ),
            WatchlistItem(
                symbol="700.HK",
                market="HK",
                alias="Tencent",
            ),
        ]
        db.add_all(items)
        db.add(
            WatchlistScore(
                symbol="700.HK",
                market="HK",
                score=0,
                confidence=0,
                recommended_action="AVOID",
                source="quant_error_v4",
                rationale="current HK data error",
                created_at=_NOW - timedelta(minutes=30),
                expires_at=_NOW + timedelta(days=7),
            )
        )
        db.commit()
        broker = _Broker()
        service_class = WatchlistQuantService
        monkeypatch.setattr(
            watchlist_api,
            "get_runner",
            lambda: SimpleNamespace(broker=broker),
        )
        monkeypatch.setattr(
            watchlist_api,
            "WatchlistQuantService",
            lambda service_db, service_broker: service_class(
                service_db,
                service_broker,
                now=_NOW,
            ),
        )

        response = watchlist_api.rank_watchlist_quantitatively(
            ttl_minutes=360,
            db=db,
        )

        assert broker.quote_requests == [["AAPL.US"]]
        by_symbol = {
            row.symbol: row
            for row in response.scores
        }
        assert set(by_symbol) == {"AAPL.US", "700.HK"}
        assert by_symbol["AAPL.US"].source == "quant_v4"
        assert by_symbol["700.HK"].source == "quant_error_v4"
    finally:
        db.close()


def test_current_quant_snapshot_excludes_retained_legacy_history() -> None:
    db = _db()
    try:
        db.add_all(
            [
                WatchlistScore(
                    symbol="AAPL.US",
                    market="US",
                    score=90,
                    confidence=0.9,
                    recommended_action="CANDIDATE",
                    source="quant_v3",
                    rationale="retained previous-generation score",
                    created_at=_NOW - timedelta(minutes=5),
                    expires_at=_NOW + timedelta(days=1),
                ),
                WatchlistScore(
                    symbol="AAPL.US",
                    market="US",
                    score=70,
                    confidence=0.8,
                    recommended_action="CANDIDATE",
                    source="quant_v4",
                    rationale="current score",
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(days=1),
                ),
                WatchlistScore(
                    symbol="MSFT.US",
                    market="US",
                    score=85,
                    confidence=0.9,
                    recommended_action="CANDIDATE",
                    source="quant_v3",
                    rationale="previous generation only",
                    created_at=_NOW,
                    expires_at=_NOW + timedelta(days=1),
                ),
            ]
        )
        db.commit()

        rows = quant_module.list_latest_current_quant_scores(db)

        assert [(row.symbol, row.source) for row in rows] == [
            ("AAPL.US", "quant_v4")
        ]
        assert db.query(WatchlistScore).count() == 3
    finally:
        db.close()


def test_due_scoring_skips_fresh_symbols_and_scores_missing_rows() -> None:
    db = _db()
    try:
        items = [
            WatchlistItem(
                symbol="AAPL.US",
                market="US",
                alias="Apple",
            ),
            WatchlistItem(
                symbol="MSFT.US",
                market="US",
                alias="Microsoft",
            ),
        ]
        db.add_all(items)
        db.add(
            WatchlistScore(
                symbol="AAPL.US",
                market="US",
                score=70,
                confidence=0.8,
                recommended_action="CANDIDATE",
                source="quant_v4",
                rationale="fresh current score",
                created_at=_NOW - timedelta(minutes=5),
                expires_at=_NOW + timedelta(minutes=25),
            )
        )
        db.commit()
        broker = _Broker()

        rows = WatchlistQuantService(
            db,
            broker,
            now=_NOW,
        ).score_due_items(items, ttl_minutes=30)

        assert broker.quote_requests == [["MSFT.US"]]
        assert [row.symbol for row in rows] == ["MSFT.US"]
        assert rows[0].source == "quant_v4"
    finally:
        db.close()


def test_due_scoring_batches_missing_then_oldest_scores() -> None:
    db = _db()
    try:
        items = [
            WatchlistItem(
                symbol="MSFT.US",
                market="US",
                alias="Microsoft",
            ),
            WatchlistItem(
                symbol="AAPL.US",
                market="US",
                alias="Apple",
            ),
            WatchlistItem(
                symbol="NVDA.US",
                market="US",
                alias="NVIDIA",
            ),
        ]
        db.add_all(items)
        db.add(
            WatchlistScore(
                symbol="MSFT.US",
                market="US",
                score=50,
                confidence=0.7,
                recommended_action="WATCH",
                source="quant_v4",
                rationale="oldest due score",
                created_at=_NOW - timedelta(minutes=90),
                expires_at=_NOW + timedelta(hours=1),
            )
        )
        db.add(
            WatchlistScore(
                symbol="AAPL.US",
                market="US",
                score=50,
                confidence=0.7,
                recommended_action="WATCH",
                source="quant_v4",
                rationale="newer due score",
                created_at=_NOW - timedelta(minutes=60),
                expires_at=_NOW + timedelta(hours=1),
            )
        )
        db.commit()
        broker = _Broker()

        rows = WatchlistQuantService(
            db,
            broker,
            now=_NOW,
        ).score_due_items(
            items,
            refresh_interval_minutes=30,
            ttl_minutes=1_440,
            max_items=2,
        )

        assert broker.quote_requests == [["NVDA.US", "MSFT.US"]]
        assert {row.symbol for row in rows} == {
            "MSFT.US",
            "NVDA.US",
        }
    finally:
        db.close()


def test_due_scoring_rejects_non_positive_batch_size() -> None:
    db = _db()
    try:
        with pytest.raises(ValueError, match="max_items must be positive"):
            WatchlistQuantService(
                db,
                _Broker(),
                now=_NOW,
            ).score_due_items([], max_items=0)
    finally:
        db.close()


def test_due_scoring_refreshes_on_cadence_before_evidence_expires() -> None:
    db = _db()
    try:
        item = WatchlistItem(
            symbol="AAPL.US",
            market="US",
            alias="Apple",
        )
        db.add(item)
        db.add(
            WatchlistScore(
                symbol="AAPL.US",
                market="US",
                score=70,
                confidence=0.8,
                recommended_action="CANDIDATE",
                source="quant_v4",
                rationale="valid but due for refresh",
                created_at=_NOW - timedelta(minutes=31),
                expires_at=_NOW + timedelta(hours=23),
            )
        )
        db.commit()
        broker = _Broker()

        rows = WatchlistQuantService(
            db,
            broker,
            now=_NOW,
        ).score_due_items(
            [item],
            refresh_interval_minutes=30,
            ttl_minutes=1_440,
        )

        assert broker.quote_requests == [["AAPL.US"]]
        assert [row.symbol for row in rows] == ["AAPL.US"]
        assert (
            rows[0].expires_at - rows[0].created_at
            == timedelta(days=1)
        )
    finally:
        db.close()


def test_due_scoring_retries_current_data_error_after_five_minutes() -> None:
    db = _db()
    try:
        item = WatchlistItem(
            symbol="AAPL.US",
            market="US",
            alias="Apple",
        )
        db.add(item)
        db.add(
            WatchlistScore(
                symbol="AAPL.US",
                market="US",
                score=0,
                confidence=0,
                recommended_action="AVOID",
                source="quant_error_v4",
                rationale="temporary BBO gap",
                created_at=_NOW - timedelta(minutes=6),
                expires_at=_NOW + timedelta(hours=23),
            )
        )
        db.commit()
        broker = _Broker()

        rows = WatchlistQuantService(
            db,
            broker,
            now=_NOW,
        ).score_due_items(
            [item],
            refresh_interval_minutes=30,
            ttl_minutes=1_440,
        )

        assert broker.quote_requests == [["AAPL.US"]]
        assert [row.symbol for row in rows] == ["AAPL.US"]
    finally:
        db.close()


def test_due_scoring_is_silent_outside_regular_hours() -> None:
    db = _db()
    try:
        item = WatchlistItem(
            symbol="AAPL.US",
            market="US",
            alias="Apple",
        )
        db.add(item)
        db.commit()
        broker = _Broker()

        rows = WatchlistQuantService(
            db,
            broker,
            now=datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
        ).score_due_items([item], ttl_minutes=30)

        assert rows == []
        assert broker.quote_requests == []
    finally:
        db.close()


class _OldLastTradeBroker(_Broker):
    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        self.quote_requests.append(list(symbols))
        return [
            Quote(
                symbol=symbol,
                last_price=100,
                bid=99.99,
                ask=100.01,
                timestamp=(
                    _NOW - timedelta(minutes=2)
                ).isoformat(),
            )
            for symbol in symbols
        ]


def test_service_does_not_treat_last_trade_age_as_bbo_age() -> None:
    db = _db()
    try:
        item = WatchlistItem(
            symbol="AAPL.US",
            market="US",
            alias="Apple",
        )
        db.add(item)
        db.commit()

        rows = WatchlistQuantService(
            db,
            _OldLastTradeBroker(),
            now=_NOW,
        ).score_items([item])

        assert len(rows) == 1
        assert rows[0].source == "quant_v4"
        assert rows[0].score > 0
        assert "STALE_BBO" not in rows[0].rationale
    finally:
        db.close()
