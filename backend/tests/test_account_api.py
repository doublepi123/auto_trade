import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_account_api_{os.getpid()}.db"

from decimal import Decimal
from unittest.mock import MagicMock, patch
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.broker import AccountInfo, BrokerGateway, CashBalance, NetAsset, Position, Quote
from app.database import engine as db_engine, SessionLocal
from app.main import app
from app.models import Base
from app.runner import AppRunner, get_runner
from app.schemas import AccountResponse, CashBalanceSchema, PositionSchema

Base.metadata.create_all(bind=db_engine)

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_runner_and_cache():
    import threading
    import app.runner as runner_mod
    import app.api.trade as trade_api
    old = runner_mod._runner
    old_refresh_lock = trade_api._account_refresh_lock
    runner_mod._runner = AppRunner()
    trade_api._account_snapshot_cache = None
    trade_api._account_refresh_lock = threading.Lock()
    yield
    import logging
    _logger = logging.getLogger(__name__)
    if trade_api._account_refresh_lock.locked():
        _logger.warning("_account_refresh_lock still held in teardown")
    runner_mod._runner = old
    trade_api._account_snapshot_cache = None
    trade_api._account_refresh_lock = old_refresh_lock


class TestAccountResponseSchema:
    def test_account_response_fields(self):
        resp = AccountResponse(
            total_assets=10000.0,
            cash_balances=[CashBalanceSchema(currency="USD", available_cash=5000.0, frozen_cash=1000.0)],
            positions=[PositionSchema(symbol="AAPL.US", side="LONG", quantity=10.0, avg_price=150.0, market_value=1500.0)],
        )
        assert resp.total_assets == 10000.0
        assert len(resp.cash_balances) == 1
        assert resp.cash_balances[0].currency == "USD"
        assert len(resp.positions) == 1
        assert resp.positions[0].symbol == "AAPL.US"

    def test_account_response_empty(self):
        resp = AccountResponse(total_assets=0.0, cash_balances=[], positions=[])
        assert resp.total_assets == 0.0
        assert resp.cash_balances == []
        assert resp.positions == []

    def test_account_response_multiple_positions(self):
        positions = [
            PositionSchema(symbol="AAPL.US", side="LONG", quantity=10.0, avg_price=150.0, market_value=1500.0),
            PositionSchema(symbol="TSLA.US", side="LONG", quantity=5.0, avg_price=200.0, market_value=1000.0),
        ]
        resp = AccountResponse(total_assets=2500.0, cash_balances=[], positions=positions)
        assert len(resp.positions) == 2

    def test_cash_balance_schema_fields(self):
        cb = CashBalanceSchema(currency="HKD", available_cash=39000.0, frozen_cash=500.0)
        assert cb.currency == "HKD"
        assert cb.available_cash == 39000.0
        assert cb.frozen_cash == 500.0

    def test_position_schema_fields(self):
        pos = PositionSchema(symbol="700.HK", side="LONG", quantity=100.0, avg_price=457.53, market_value=45753.0)
        assert pos.symbol == "700.HK"
        assert pos.side == "LONG"
        assert pos.quantity == 100.0
        assert pos.avg_price == 457.53
        assert pos.market_value == 45753.0

    def test_account_response_serialization(self):
        resp = AccountResponse(
            total_assets=50000.0,
            cash_balances=[CashBalanceSchema(currency="USD", available_cash=1000.0, frozen_cash=200.0)],
            positions=[PositionSchema(symbol="AAPL.US", side="LONG", quantity=10.0, avg_price=150.0, market_value=1500.0)],
        )
        data = resp.model_dump()
        assert data["total_assets"] == 50000.0
        assert data["cash_balances"][0]["currency"] == "USD"
        assert data["positions"][0]["market_value"] == 1500.0


class TestGetAccountEndpointSuccess:
    def test_account_endpoint_returns_correct_structure(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("200"))],
            net_assets=[NetAsset(currency="USD", amount=Decimal("50000"))],
        )
        mock_broker.get_positions.return_value = [
            Position(symbol="AAPL.US", side="LONG", quantity=Decimal("10"), avg_price=Decimal("150")),
        ]
        mock_broker.get_quotes.return_value = [
            Quote(symbol="AAPL.US", last_price=155.0, bid=154.5, ask=155.5, timestamp="2026-01-01"),
        ]
        runner.broker = mock_broker

        resp = client.get("/api/account")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_assets"] == 50000.0
        assert len(data["cash_balances"]) == 1
        assert data["cash_balances"][0]["currency"] == "USD"
        assert data["cash_balances"][0]["available_cash"] == 1000.0
        assert data["cash_balances"][0]["frozen_cash"] == 200.0
        assert len(data["positions"]) == 1
        assert data["positions"][0]["symbol"] == "AAPL.US"
        assert data["positions"][0]["side"] == "LONG"
        assert data["positions"][0]["quantity"] == 10.0
        assert data["positions"][0]["avg_price"] == 150.0
        assert data["positions"][0]["market_value"] == pytest.approx(1550.0)

    def test_account_endpoint_market_value_uses_quote(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("100000"),
            cash_balances=[],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = [
            Position(symbol="TSLA.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("200")),
        ]
        mock_broker.get_quotes.return_value = [
            Quote(symbol="TSLA.US", last_price=250.0, bid=249.5, ask=250.5, timestamp=""),
        ]
        runner.broker = mock_broker

        resp = client.get("/api/account")
        data = resp.json()
        assert data["positions"][0]["market_value"] == pytest.approx(1250.0)

    def test_account_endpoint_multiple_positions(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("100000"),
            cash_balances=[
                CashBalance(currency="USD", available_cash=Decimal("5000"), frozen_cash=Decimal("1000")),
                CashBalance(currency="HKD", available_cash=Decimal("39000"), frozen_cash=Decimal("500")),
            ],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = [
            Position(symbol="AAPL.US", side="LONG", quantity=Decimal("10"), avg_price=Decimal("150")),
            Position(symbol="TSLA.US", side="SHORT", quantity=Decimal("5"), avg_price=Decimal("200")),
        ]
        mock_broker.get_quotes.return_value = [
            Quote(symbol="AAPL.US", last_price=160.0, bid=159.5, ask=160.5, timestamp=""),
            Quote(symbol="TSLA.US", last_price=190.0, bid=189.5, ask=190.5, timestamp=""),
        ]
        runner.broker = mock_broker

        resp = client.get("/api/account")
        data = resp.json()
        assert len(data["cash_balances"]) == 2
        assert len(data["positions"]) == 2

    def test_account_endpoint_empty_positions(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("10000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("10000"), frozen_cash=Decimal("0"))],
            net_assets=[NetAsset(currency="USD", amount=Decimal("10000"))],
        )
        mock_broker.get_positions.return_value = []
        runner.broker = mock_broker

        resp = client.get("/api/account")
        data = resp.json()
        assert data["total_assets"] == 10000.0
        assert data["positions"] == []


class TestGetAccountEndpointBrokerFailure:
    def test_account_endpoint_broker_get_account_fails(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.side_effect = Exception("connection failed")
        mock_broker.get_positions.return_value = []
        runner.broker = mock_broker

        resp = client.get("/api/account")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_assets"] == 0.0
        assert data["cash_balances"] == []

    def test_account_endpoint_broker_get_account_fails_positions_still_work(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.side_effect = Exception("connection failed")
        mock_broker.get_positions.return_value = [
            Position(symbol="AAPL.US", side="LONG", quantity=Decimal("10"), avg_price=Decimal("150")),
        ]
        mock_broker.get_quotes.return_value = [
            Quote(symbol="AAPL.US", last_price=155.0, bid=154.5, ask=155.5, timestamp=""),
        ]
        runner.broker = mock_broker

        resp = client.get("/api/account")
        data = resp.json()
        assert data["total_assets"] == 0.0
        assert data["cash_balances"] == []
        assert len(data["positions"]) == 1
        assert data["positions"][0]["market_value"] == pytest.approx(1550.0)

    def test_account_endpoint_broker_get_positions_fails(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("200"))],
            net_assets=[],
        )
        mock_broker.get_positions.side_effect = Exception("positions unavailable")
        runner.broker = mock_broker

        resp = client.get("/api/account")
        data = resp.json()
        assert data["total_assets"] == 50000.0
        assert data["positions"] == []

    def test_account_endpoint_both_broker_calls_fail(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.side_effect = Exception("connection failed")
        mock_broker.get_positions.side_effect = Exception("connection failed")
        runner.broker = mock_broker

        resp = client.get("/api/account")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_assets"] == 0.0
        assert data["cash_balances"] == []
        assert data["positions"] == []


class TestGetAccountEndpointQuoteFallback:
    def test_account_endpoint_quote_fails_falls_back_to_avg_price(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = [
            Position(symbol="AAPL.US", side="LONG", quantity=Decimal("10"), avg_price=Decimal("150")),
        ]
        mock_broker.get_quotes.side_effect = Exception("quote unavailable")
        runner.broker = mock_broker

        resp = client.get("/api/account")
        data = resp.json()
        assert data["positions"][0]["market_value"] == pytest.approx(1500.0)

    def test_account_endpoint_mixed_quote_success_and_failure(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = [
            Position(symbol="AAPL.US", side="LONG", quantity=Decimal("10"), avg_price=Decimal("150")),
            Position(symbol="TSLA.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("200")),
        ]

        # Batch quote returns only AAPL successfully; TSLA quote omitted, endpoint
        # must fall back to avg_price * quantity for it.
        mock_broker.get_quotes.return_value = [
            Quote(symbol="AAPL.US", last_price=160.0, bid=159.5, ask=160.5, timestamp=""),
        ]
        runner.broker = mock_broker

        resp = client.get("/api/account")
        data = resp.json()
        aapl = next(p for p in data["positions"] if p["symbol"] == "AAPL.US")
        tsla = next(p for p in data["positions"] if p["symbol"] == "TSLA.US")
        assert aapl["market_value"] == pytest.approx(1600.0)
        assert tsla["market_value"] == pytest.approx(1000.0)

    def test_account_endpoint_total_assets_from_primary_currency(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[
                CashBalance(currency="CNH", available_cash=Decimal("7200"), frozen_cash=Decimal("0")),
                CashBalance(currency="USD", available_cash=Decimal("5000"), frozen_cash=Decimal("1000")),
            ],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = []
        runner.broker = mock_broker

        resp = client.get("/api/account")
        data = resp.json()
        assert data["total_assets"] == 50000.0


class TestGetAccountEndpointCache:
    def test_account_endpoint_uses_cached_snapshot_within_ttl(self, monkeypatch):
        import app.api.trade as trade_api

        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("0"))],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = []
        runner.broker = mock_broker

        trade_api._account_snapshot_cache = None
        monkeypatch.setattr(trade_api, "_account_cache_now", lambda: 1000.0)

        first = client.get("/api/account")
        second = client.get("/api/account")

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["total_assets"] == 50000.0
        assert mock_broker.get_account.call_count == 1
        assert mock_broker.get_positions.call_count == 1

    def test_account_endpoint_refreshes_after_ttl(self, monkeypatch):
        import app.api.trade as trade_api

        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"), cash_balances=[], net_assets=[]
        )
        mock_broker.get_positions.return_value = []
        runner.broker = mock_broker
        trade_api._account_snapshot_cache = None

        now = {"value": 1000.0}
        monkeypatch.setattr(trade_api, "_account_cache_now", lambda: now["value"])
        client.get("/api/account")
        now["value"] += trade_api.ACCOUNT_CACHE_TTL_SECONDS + 0.1
        client.get("/api/account")

        assert mock_broker.get_account.call_count == 2
        assert mock_broker.get_positions.call_count == 2

    def test_account_endpoint_returns_cache_when_refresh_fails(self, monkeypatch):
        import app.api.trade as trade_api

        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("0"))],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = []
        runner.broker = mock_broker
        trade_api._account_snapshot_cache = None

        now = {"value": 1000.0}
        monkeypatch.setattr(trade_api, "_account_cache_now", lambda: now["value"])
        client.get("/api/account")
        now["value"] += trade_api.ACCOUNT_CACHE_TTL_SECONDS + 0.1
        mock_broker.get_account.side_effect = RuntimeError("broker down")
        mock_broker.get_positions.side_effect = RuntimeError("broker down")

        resp = client.get("/api/account")
        data = resp.json()
        assert resp.status_code == 200
        assert data["available"] is True
        assert data["total_assets"] == 50000.0
        assert data["error"] is None

    def test_account_endpoint_reports_unavailable_without_cache(self, monkeypatch):
        import app.api.trade as trade_api

        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.side_effect = RuntimeError("broker down")
        mock_broker.get_positions.side_effect = RuntimeError("broker down")
        runner.broker = mock_broker
        trade_api._account_snapshot_cache = None
        monkeypatch.setattr(trade_api, "_account_cache_now", lambda: 1000.0)

        resp = client.get("/api/account")
        data = resp.json()
        assert resp.status_code == 200
        assert data["available"] is False
        assert data["error"] == "Account data unavailable"

    def test_account_endpoint_stampede_protection(self, monkeypatch):
        """Cold-cache concurrent requests: second waiter uses first's result."""
        import app.api.trade as trade_api
        import threading
        import time

        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("0"))],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = []
        runner.broker = mock_broker
        trade_api._account_snapshot_cache = None
        monkeypatch.setattr(trade_api, "_account_cache_now", lambda: 1000.0)

        entered_fetch = threading.Event()
        release_fetch = threading.Event()
        original_fetch = trade_api._fetch_account_response

        def gated_fetch():
            entered_fetch.set()
            assert release_fetch.wait(timeout=5.0), "timed out waiting for release"
            return original_fetch()

        monkeypatch.setattr(trade_api, "_fetch_account_response", gated_fetch)

        results: dict[str, tuple[int, dict[str, Any]]] = {}

        def call_endpoint(label: str) -> None:
            resp = client.get("/api/account")
            results[label] = (resp.status_code, resp.json())

        t1 = threading.Thread(target=call_endpoint, args=("first",), daemon=True)
        t2 = threading.Thread(target=call_endpoint, args=("second",), daemon=True)

        t1.start()
        assert entered_fetch.wait(timeout=5.0), "t1 never entered fetch"

        t2.start()
        time.sleep(0.3)  # let t2 reach the blocking acquire

        release_fetch.set()

        t1.join(timeout=5.0)
        t2.join(timeout=5.0)
        assert not t1.is_alive(), "t1 did not finish"
        assert not t2.is_alive(), "t2 did not finish"

        assert results["first"][0] == 200
        assert results["second"][0] == 200
        assert results["first"][1]["total_assets"] == 50000.0
        assert results["second"][1]["total_assets"] == 50000.0
        assert mock_broker.get_account.call_count == 1
        assert mock_broker.get_positions.call_count == 1