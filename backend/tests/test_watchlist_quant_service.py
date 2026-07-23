from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.broker import BrokerCandle, Quote
from app.models import Base, WatchlistItem, WatchlistScore
from app.services.watchlist_quant_service import (
    WatchlistQuantService,
    build_watchlist_quant_metrics,
    score_watchlist_quant_metrics,
)

_NOW = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)


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
                volume=2_000_000,
            )
        )
        price = close
    return result


def _intraday_bars() -> list[BrokerCandle]:
    result: list[BrokerCandle] = []
    price = 100.0
    end = _NOW - timedelta(minutes=5)
    for index in range(700):
        close = price * (1.001 if index % 2 == 0 else 0.999)
        result.append(
            BrokerCandle(
                timestamp=end - timedelta(minutes=5 * (699 - index)),
                open=price,
                high=max(price, close) * 1.0002,
                low=min(price, close) * 0.9998,
                close=close,
                volume=100_000,
            )
        )
        price = close
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
    )

    score = score_watchlist_quant_metrics(metrics)

    assert metrics.blockers == ()
    assert metrics.intraday_autocorrelation < 0
    assert metrics.intraday_reversal_rate > 0.9
    assert score.score >= 50
    assert score.recommended_action == "CANDIDATE"
    assert score.rationale.startswith("quant-v1;")


def test_quant_score_caps_candidate_with_hard_data_blockers() -> None:
    metrics = build_watchlist_quant_metrics(
        symbol="THIN.US",
        market="US",
        daily=_daily_bars()[:10],
        intraday=_intraday_bars()[:20],
        quote=None,
    )

    score = score_watchlist_quant_metrics(metrics)

    assert "INSUFFICIENT_DAILY_DATA" in metrics.blockers
    assert "INSUFFICIENT_INTRADAY_DATA" in metrics.blockers
    assert "MISSING_BBO" in metrics.blockers
    assert score.score <= 39
    assert score.recommended_action == "AVOID"


class _Broker:
    def __init__(self, *, fail_symbol: str = "") -> None:
        self.fail_symbol = fail_symbol

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
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
        assert by_symbol["AAPL.US"].source == "quant_v1"
        assert by_symbol["BROKEN.US"].source == "quant_error"
        assert by_symbol["BROKEN.US"].recommended_action == "AVOID"
        assert db.query(WatchlistScore).count() == 2
    finally:
        db.close()
