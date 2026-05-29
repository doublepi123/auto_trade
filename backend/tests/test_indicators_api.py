import os
import tempfile
os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/test_indicators_api_{os.getpid()}.db"
)

from datetime import datetime, timezone, timedelta

from app.database import engine as db_engine, SessionLocal
from app.models import Base, StrategyConfig
from app.main import app
from app.api.indicators import get_indicator_broker
from fastapi.testclient import TestClient
import pytest

Base.metadata.create_all(bind=db_engine)
client = TestClient(app)


class _FakeCandle:
    def __init__(self, close: float, volume: float = 1000.0, offset_days: int = 0) -> None:
        self.open = close
        self.high = close * 1.01
        self.low = close * 0.99
        self.close = close
        self.volume = volume
        self.timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=offset_days)


class _FakeBroker:
    def get_candlesticks(self, symbol, period, count):
        return [_FakeCandle(100.0 + i, offset_days=i) for i in range(40)]

    def get_quote(self, symbol):
        class Q:
            last_price = 139.0
        return Q()

    def close(self):
        pass


class _EmptyBroker:
    def get_candlesticks(self, symbol, period, count):
        return []

    def get_quote(self, symbol):
        raise RuntimeError("no quote")

    def close(self):
        pass


@pytest.fixture
def clean_db():
    db = SessionLocal()
    db.query(StrategyConfig).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(StrategyConfig).delete()
    db.commit()
    db.close()
    app.dependency_overrides.pop(get_indicator_broker, None)


def _set_config(symbol="AAPL.US", market="US"):
    db = SessionLocal()
    db.add(StrategyConfig(symbol=symbol, market=market))
    db.commit()
    db.close()


class TestIndicatorsApi:
    def test_available_with_candles(self, clean_db):
        _set_config()
        app.dependency_overrides[get_indicator_broker] = lambda: _FakeBroker()
        resp = client.get("/api/indicators", params={"symbol": "AAPL.US"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        assert body["symbol"] == "AAPL.US"
        assert body["rsi"] is not None
        assert set(body["macd"].keys()) == {"macd", "signal", "histogram"}
        assert set(body["multi_timeframe"].keys()) == {
            "daily_trend", "minute_trend", "aligned", "description",
        }

    def test_unavailable_when_no_candles(self, clean_db):
        _set_config()
        app.dependency_overrides[get_indicator_broker] = lambda: _EmptyBroker()
        resp = client.get("/api/indicators", params={"symbol": "AAPL.US"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is False
        assert body["rsi"] is None
        assert body["macd"] is None

    def test_symbol_defaults_to_config(self, clean_db):
        _set_config(symbol="TSLA.US")
        app.dependency_overrides[get_indicator_broker] = lambda: _FakeBroker()
        resp = client.get("/api/indicators")
        assert resp.status_code == 200
        assert resp.json()["symbol"] == "TSLA.US"

    def test_422_when_no_symbol_and_no_config(self, clean_db):
        app.dependency_overrides[get_indicator_broker] = lambda: _FakeBroker()
        resp = client.get("/api/indicators")
        assert resp.status_code == 422
