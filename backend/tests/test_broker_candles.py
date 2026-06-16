"""Broker candlesticks → backtest — API. Monkeypatches get_runner; no real broker."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.core.broker import BrokerCandle
from app.main import app

client = TestClient(app)


def _candle(i: int, o: float, h: float, l: float, c: float, v: float = 1000.0) -> BrokerCandle:
    return BrokerCandle(
        timestamp=datetime(2026, 6, 16, 10, i, tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c, volume=v,
    )


class FakeBroker:
    def __init__(self, candles: list[BrokerCandle], raise_on_call: bool = False) -> None:
        self._candles = candles
        self._raise = raise_on_call

    def get_candlesticks(self, symbol: str, period: str, count: int) -> list[BrokerCandle]:
        if self._raise:
            raise RuntimeError("broker down")
        return list(self._candles)


class FakeRunner:
    def __init__(self, broker: object | None) -> None:
        self.broker = broker


def test_candles_success() -> None:
    candles = [_candle(0, 100, 110, 95, 105), _candle(1, 105, 120, 100, 115)]
    import app.api.broker as mod
    orig = mod.get_runner
    mod.get_runner = lambda: FakeRunner(FakeBroker(candles))  # type: ignore[assignment]
    try:
        resp = client.get("/api/broker/candles", params={"symbol": "AAPL.US", "period": "DAY", "count": 2})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["count"] == 2
        assert data["symbol"] == "AAPL.US"
        assert "timestamp,open,high,low,close,volume" in data["csv_text"]
        assert data["csv_text"].count("\n") >= 2
    finally:
        mod.get_runner = orig  # type: ignore[assignment]


def test_candles_filters_invalid_bars() -> None:
    # Second candle has high<low -> invalid; third has close=0 -> invalid.
    candles = [
        _candle(0, 100, 110, 95, 105),
        _candle(1, 100, 90, 110, 105),   # high<low
        _candle(2, 0, 0, 0, 0),          # non-positive
    ]
    import app.api.broker as mod
    orig = mod.get_runner
    mod.get_runner = lambda: FakeRunner(FakeBroker(candles))  # type: ignore[assignment]
    try:
        resp = client.get("/api/broker/candles", params={"symbol": "AAPL.US"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 1
    finally:
        mod.get_runner = orig  # type: ignore[assignment]


def test_candles_bad_period_422() -> None:
    resp = client.get("/api/broker/candles", params={"symbol": "AAPL.US", "period": "NOPE"})
    assert resp.status_code == 422


def test_candles_no_broker_503() -> None:
    import app.api.broker as mod
    orig = mod.get_runner
    mod.get_runner = lambda: FakeRunner(None)  # type: ignore[assignment]
    try:
        resp = client.get("/api/broker/candles", params={"symbol": "AAPL.US"})
        assert resp.status_code == 503
    finally:
        mod.get_runner = orig  # type: ignore[assignment]


def test_candles_broker_failure_503() -> None:
    import app.api.broker as mod
    orig = mod.get_runner
    mod.get_runner = lambda: FakeRunner(FakeBroker([], raise_on_call=True))  # type: ignore[assignment]
    try:
        resp = client.get("/api/broker/candles", params={"symbol": "AAPL.US"})
        assert resp.status_code == 503
    finally:
        mod.get_runner = orig  # type: ignore[assignment]
