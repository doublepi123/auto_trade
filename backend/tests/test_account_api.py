import os
import threading

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_account_api.db"

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.broker import AccountInfo, BrokerGateway, CashBalance, NetAsset, Position, Quote
from app.database import engine as db_engine, SessionLocal
from app.main import app
from app.models import Base
from app.runner import AppRunner, get_runner
from app.schemas import AccountResponse, CashBalanceSchema, PositionSchema
from app.services.account_snapshot_service import clear_account_snapshot_cache

Base.metadata.create_all(bind=db_engine)

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_runner():
    import app.runner as runner_mod
    clear_account_snapshot_cache()
    old = runner_mod._runner
    runner_mod._runner = AppRunner()
    yield
    runner_mod._runner = old
    clear_account_snapshot_cache()


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
        mock_broker.get_quote.return_value = Quote(
            symbol="AAPL.US", last_price=155.0, bid=154.5, ask=155.5, timestamp="2026-01-01",
        )
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
        mock_broker.get_quote.return_value = Quote(
            symbol="TSLA.US", last_price=250.0, bid=249.5, ask=250.5, timestamp="",
        )
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
        mock_broker.get_quote.side_effect = [
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


class TestGetAccountEndpointSnapshotCache:
    def test_account_endpoint_reuses_successful_snapshot_within_ttl(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("200"))],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = [
            Position(symbol="AAPL.US", side="LONG", quantity=Decimal("10"), avg_price=Decimal("150")),
        ]
        mock_broker.get_quote.return_value = Quote(
            symbol="AAPL.US", last_price=155.0, bid=154.5, ask=155.5, timestamp="",
        )
        runner.broker = mock_broker

        first = client.get("/api/account")
        second = client.get("/api/account")

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json() == first.json()
        assert mock_broker.get_account.call_count == 1
        assert mock_broker.get_positions.call_count == 1
        assert mock_broker.get_quote.call_count == 1

    def test_account_endpoint_returns_stale_snapshot_when_refresh_fails(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.side_effect = [
            AccountInfo(
                total_assets=Decimal("50000"),
                cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("200"))],
                net_assets=[],
            ),
            Exception("connection failed"),
        ]
        mock_broker.get_positions.side_effect = [
            [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("10"), avg_price=Decimal("150"))],
            Exception("positions unavailable"),
        ]
        mock_broker.get_quote.return_value = Quote(
            symbol="AAPL.US", last_price=155.0, bid=154.5, ask=155.5, timestamp="",
        )
        runner.broker = mock_broker

        with patch("app.services.account_snapshot_service.monotonic", side_effect=[100.0, 106.0]):
            first = client.get("/api/account")
            second = client.get("/api/account")

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json() == first.json()
        assert mock_broker.get_account.call_count == 2
        assert mock_broker.get_positions.call_count == 2

    def test_account_endpoint_refreshes_after_ttl_expires(self):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.side_effect = [
            AccountInfo(
                total_assets=Decimal("50000"),
                cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("200"))],
                net_assets=[],
            ),
            AccountInfo(
                total_assets=Decimal("60000"),
                cash_balances=[CashBalance(currency="USD", available_cash=Decimal("2000"), frozen_cash=Decimal("300"))],
                net_assets=[],
            ),
        ]
        mock_broker.get_positions.return_value = []
        runner.broker = mock_broker

        with patch("app.services.account_snapshot_service.monotonic", side_effect=[100.0, 106.0]):
            first = client.get("/api/account")
            second = client.get("/api/account")

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["total_assets"] == 50000.0
        assert second.json()["total_assets"] == 60000.0
        assert mock_broker.get_account.call_count == 2
        assert mock_broker.get_positions.call_count == 2

    def test_account_endpoint_returns_stale_snapshot_during_in_flight_refresh(self):
        runner = get_runner()
        stale_broker = MagicMock()
        stale_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("200"))],
            net_assets=[],
        )
        stale_broker.get_positions.return_value = []
        runner.broker = stale_broker

        with patch("app.services.account_snapshot_service.monotonic", return_value=100.0):
            stale_response = client.get("/api/account")

        refresh_started = threading.Event()
        release_refresh = threading.Event()
        refresh_finished = threading.Event()
        refresh_response = None

        refresh_broker = MagicMock()

        def get_account_side_effect():
            refresh_started.set()
            release_refresh.wait(timeout=2)
            return AccountInfo(
                total_assets=Decimal("60000"),
                cash_balances=[CashBalance(currency="USD", available_cash=Decimal("2000"), frozen_cash=Decimal("300"))],
                net_assets=[],
            )

        refresh_broker.get_account.side_effect = get_account_side_effect
        refresh_broker.get_positions.return_value = []
        runner.broker = refresh_broker

        def refresh_account():
            nonlocal refresh_response
            with patch("app.services.account_snapshot_service.monotonic", return_value=106.0):
                refresh_response = client.get("/api/account")
            refresh_finished.set()

        refresh_thread = threading.Thread(target=refresh_account)
        refresh_thread.start()
        assert refresh_started.wait(timeout=2)

        try:
            with patch("app.services.account_snapshot_service.monotonic", return_value=106.0):
                stale_during_refresh = client.get("/api/account")

            assert stale_during_refresh.status_code == 200
            assert stale_during_refresh.json() == stale_response.json()
            assert not refresh_finished.is_set()
        finally:
            release_refresh.set()
            refresh_thread.join(timeout=2)

        assert refresh_response is not None
        assert refresh_response.status_code == 200
        assert refresh_response.json()["total_assets"] == 60000.0
        assert refresh_broker.get_account.call_count == 1

    def test_account_endpoint_logs_snapshot_sub_step_durations(self, caplog):
        runner = get_runner()
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("200"))],
            net_assets=[],
        )
        mock_broker.get_positions.return_value = [
            Position(symbol="AAPL.US", side="LONG", quantity=Decimal("10"), avg_price=Decimal("150")),
        ]
        mock_broker.get_quote.return_value = Quote(
            symbol="AAPL.US", last_price=155.0, bid=154.5, ask=155.5, timestamp="",
        )
        runner.broker = mock_broker

        with caplog.at_level("INFO", logger="auto_trade.account_snapshot"):
            resp = client.get("/api/account")

        assert resp.status_code == 200
        messages = [record.getMessage() for record in caplog.records]
        assert any("account snapshot get_account completed in" in message for message in messages)
        assert any("account snapshot get_positions completed in" in message for message in messages)
        assert any("account snapshot get_quote AAPL.US completed in" in message for message in messages)

    def test_account_endpoint_clears_refreshing_after_unexpected_snapshot_error(self):
        runner = get_runner()
        stale_broker = MagicMock()
        stale_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("200"))],
            net_assets=[],
        )
        stale_broker.get_positions.return_value = []
        runner.broker = stale_broker

        with patch("app.services.account_snapshot_service.monotonic", return_value=100.0):
            stale_response = client.get("/api/account")

        refresh_broker = MagicMock()
        refresh_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("60000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("2000"), frozen_cash=Decimal("300"))],
            net_assets=[],
        )
        refresh_broker.get_positions.return_value = []
        runner.broker = refresh_broker

        with patch(
            "app.services.account_snapshot_service.AccountSnapshotService._load_snapshot",
            side_effect=RuntimeError("unexpected snapshot error"),
        ):
            with patch("app.services.account_snapshot_service.monotonic", return_value=106.0):
                with pytest.raises(RuntimeError, match="unexpected snapshot error"):
                    client.get("/api/account")

        with patch("app.services.account_snapshot_service.monotonic", return_value=107.0):
            refreshed_response = client.get("/api/account")

        assert refreshed_response.status_code == 200
        assert refreshed_response.json()["total_assets"] == 60000.0
        assert refreshed_response.json() != stale_response.json()
        assert refresh_broker.get_account.call_count == 1

    def test_account_endpoint_does_not_start_duplicate_refresh_before_cache_update(self):
        import app.services.account_snapshot_service as snapshot_mod

        runner = get_runner()
        stale_broker = MagicMock()
        stale_broker.get_account.return_value = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("200"))],
            net_assets=[],
        )
        stale_broker.get_positions.return_value = []
        runner.broker = stale_broker

        with patch("app.services.account_snapshot_service.monotonic", return_value=100.0):
            stale_response = client.get("/api/account")

        window_open = threading.Event()
        release_window = threading.Event()
        first_response = None
        original_lock = snapshot_mod._CACHE_LOCK

        class WindowLock:
            def __enter__(self):
                if (
                    not window_open.is_set()
                    and refresh_broker.get_account.call_count >= 1
                    and snapshot_mod._SNAPSHOT_CACHE is not None
                    and snapshot_mod._SNAPSHOT_CACHE_EXPIRES_AT <= 106.0
                ):
                    window_open.set()
                    release_window.wait(timeout=2)
                return original_lock.__enter__()

            def __exit__(self, exc_type, exc, tb):
                return original_lock.__exit__(exc_type, exc, tb)

        refresh_broker = MagicMock()
        refresh_broker.get_account.side_effect = [
            AccountInfo(
                total_assets=Decimal("60000"),
                cash_balances=[CashBalance(currency="USD", available_cash=Decimal("2000"), frozen_cash=Decimal("300"))],
                net_assets=[],
            ),
            AccountInfo(
                total_assets=Decimal("70000"),
                cash_balances=[CashBalance(currency="USD", available_cash=Decimal("3000"), frozen_cash=Decimal("400"))],
                net_assets=[],
            ),
        ]
        refresh_broker.get_positions.return_value = []
        runner.broker = refresh_broker

        def refresh_account():
            nonlocal first_response
            with patch("app.services.account_snapshot_service.monotonic", return_value=106.0):
                first_response = client.get("/api/account")

        snapshot_mod._CACHE_LOCK = WindowLock()
        refresh_thread = threading.Thread(target=refresh_account)
        refresh_thread.start()
        assert window_open.wait(timeout=2)

        try:
            with patch("app.services.account_snapshot_service.monotonic", return_value=106.0):
                second_response = client.get("/api/account")

            assert second_response.status_code == 200
            assert second_response.json() == stale_response.json()
            assert refresh_broker.get_account.call_count == 1
        finally:
            release_window.set()
            refresh_thread.join(timeout=2)
            snapshot_mod._CACHE_LOCK = original_lock

        assert first_response is not None
        assert first_response.status_code == 200
        assert first_response.json()["total_assets"] == 60000.0


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
        mock_broker.get_quote.return_value = Quote(
            symbol="AAPL.US", last_price=155.0, bid=154.5, ask=155.5, timestamp="",
        )
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
        mock_broker.get_quote.side_effect = Exception("quote unavailable")
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

        def quote_side_effect(symbol):
            if symbol == "AAPL.US":
                return Quote(symbol="AAPL.US", last_price=160.0, bid=159.5, ask=160.5, timestamp="")
            raise Exception("quote unavailable")

        mock_broker.get_quote.side_effect = quote_side_effect
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
