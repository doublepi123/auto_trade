from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.core.broker import Quote
from app.main import app


client = TestClient(app)


class _FakeBroker:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.estimate_call: tuple[
            str, str, Decimal, str
        ] | None = None

    def get_quote(self, symbol: str) -> Quote:
        if self.fail:
            raise RuntimeError("broker unavailable")
        return Quote(
            symbol=symbol,
            last_price=100.5,
            bid=100.0,
            ask=101.0,
            timestamp="2026-07-27T13:31:00Z",
        )

    def get_cash(self, currency: str) -> Decimal:
        if self.fail:
            raise RuntimeError("broker unavailable")
        return Decimal("12345.67")

    def estimate_margin_max_quantity(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        currency: str,
    ) -> Decimal:
        if self.fail:
            raise RuntimeError("broker unavailable")
        self.estimate_call = (symbol, side, price, currency)
        return Decimal("120")


class _FakeRunner:
    def __init__(self, broker: object | None) -> None:
        self.broker = broker


def test_buying_power_uses_explicit_us_limit_price(
    monkeypatch,
) -> None:
    import app.api.broker as broker_api

    broker = _FakeBroker()
    monkeypatch.setattr(
        broker_api,
        "get_runner",
        lambda: _FakeRunner(broker),
    )

    response = client.get(
        "/api/broker/buying-power",
        params={
            "symbol": "nvda.us",
            "side": "BUY",
            "price": 208.8,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["symbol"] == "NVDA.US"
    assert body["market"] == "US"
    assert body["currency"] == "USD"
    assert body["price"] == 208.8
    assert body["available_cash"] == 12345.67
    assert body["max_quantity"] == 120.0
    assert body["buying_power"] == 25056.0
    assert broker.estimate_call == (
        "NVDA.US",
        "BUY",
        Decimal("208.8"),
        "USD",
    )


def test_buying_power_uses_live_bid_for_hk_sell(
    monkeypatch,
) -> None:
    import app.api.broker as broker_api

    broker = _FakeBroker()
    monkeypatch.setattr(
        broker_api,
        "get_runner",
        lambda: _FakeRunner(broker),
    )

    response = client.get(
        "/api/broker/buying-power",
        params={"symbol": "700.HK", "side": "SELL"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["market"] == "HK"
    assert body["currency"] == "HKD"
    assert body["price"] == 100.0
    assert broker.estimate_call == (
        "700.HK",
        "SELL",
        Decimal("100.0"),
        "HKD",
    )


def test_buying_power_rejects_unsupported_market() -> None:
    response = client.get(
        "/api/broker/buying-power",
        params={"symbol": "D05.SG", "price": 10},
    )

    assert response.status_code == 422


def test_buying_power_returns_503_for_missing_or_failed_broker(
    monkeypatch,
) -> None:
    import app.api.broker as broker_api

    monkeypatch.setattr(
        broker_api,
        "get_runner",
        lambda: _FakeRunner(None),
    )
    missing = client.get(
        "/api/broker/buying-power",
        params={"symbol": "NVDA.US", "price": 100},
    )
    assert missing.status_code == 503

    monkeypatch.setattr(
        broker_api,
        "get_runner",
        lambda: _FakeRunner(_FakeBroker(fail=True)),
    )
    failed = client.get(
        "/api/broker/buying-power",
        params={"symbol": "NVDA.US", "price": 100},
    )
    assert failed.status_code == 503

    def _missing_runner() -> None:
        raise RuntimeError("runner unavailable")

    monkeypatch.setattr(
        broker_api,
        "get_runner",
        _missing_runner,
    )
    unavailable = client.get(
        "/api/broker/buying-power",
        params={"symbol": "NVDA.US", "price": 100},
    )
    assert unavailable.status_code == 503
