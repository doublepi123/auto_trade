from decimal import Decimal

from app.core import broker as broker_module
from app.core.broker import (
    AccountInfo,
    BrokerCredentials,
    BrokerGateway,
    CashBalance,
    NetAsset,
    OrderResult,
    Position,
    Quote,
    _import_openapi,
    _SIDE_MAP,
)


class TestQuote:
    def test_quote_dataclass(self) -> None:
        q = Quote(symbol="AAPL.US", last_price=150.0, bid=149.5, ask=150.5, timestamp="2026-01-01")
        assert q.symbol == "AAPL.US"
        assert q.last_price == 150.0
        assert q.bid == 149.5
        assert q.ask == 150.5

    def test_quote_defaults(self) -> None:
        q = Quote(symbol="TSLA.US", last_price=0, bid=0, ask=0, timestamp="")
        assert q.last_price == 0


class TestBrokerGateway:
    def test_init_no_credentials(self) -> None:
        gw = BrokerGateway()
        assert gw._quote_ctx is None
        assert gw._trade_ctx is None

    def test_init_prefers_explicit_credentials(self, monkeypatch) -> None:
        captured: dict[str, tuple[str, str, str]] = {}

        class FakeConfig:
            @staticmethod
            def from_apikey(app_key: str, app_secret: str, access_token: str) -> tuple[str, str, str]:
                captured["args"] = (app_key, app_secret, access_token)
                return app_key, app_secret, access_token

        class FakeModule:
            Config = FakeConfig

            class QuoteContext:
                def __init__(self, config: object) -> None:
                    self.config = config

            class TradeContext:
                def __init__(self, config: object) -> None:
                    self.config = config

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        monkeypatch.setattr(broker_module.settings, "longbridge_app_key", "settings-key", raising=False)
        monkeypatch.setattr(broker_module.settings, "longbridge_app_secret", "settings-secret", raising=False)
        monkeypatch.setattr(broker_module.settings, "longbridge_access_token", "settings-token", raising=False)

        gw = BrokerGateway(BrokerCredentials(app_key="db-key", app_secret="db-secret", access_token="db-token"))
        gw._init_clients()

        assert captured["args"] == ("db-key", "db-secret", "db-token")

    def test_init_falls_back_to_settings(self, monkeypatch) -> None:
        captured: dict[str, tuple[str, str, str]] = {}

        class FakeConfig:
            @staticmethod
            def from_apikey(app_key: str, app_secret: str, access_token: str) -> tuple[str, str, str]:
                captured["args"] = (app_key, app_secret, access_token)
                return app_key, app_secret, access_token

        class FakeModule:
            Config = FakeConfig

            class QuoteContext:
                def __init__(self, config: object) -> None:
                    self.config = config

            class TradeContext:
                def __init__(self, config: object) -> None:
                    self.config = config

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        monkeypatch.setattr(broker_module.settings, "longbridge_app_key", "settings-key", raising=False)
        monkeypatch.setattr(broker_module.settings, "longbridge_app_secret", "settings-secret", raising=False)
        monkeypatch.setattr(broker_module.settings, "longbridge_access_token", "settings-token", raising=False)

        gw = BrokerGateway()
        gw._init_clients()

        assert captured["args"] == ("settings-key", "settings-secret", "settings-token")

    def test_quote_callbacks_registration(self) -> None:
        gw = BrokerGateway()
        received: list[Quote] = []

        def cb(q: Quote) -> None:
            received.append(q)

        gw._quote_callbacks.append(cb)
        assert len(gw._quote_callbacks) == 1

        test_q = Quote(symbol="NVDA.US", last_price=120.0, bid=119.5, ask=120.5, timestamp="")
        gw._quote_callbacks[0](test_q)
        assert len(received) == 1
        assert received[0].last_price == 120.0

    def test_get_positions_flattens_stock_position_channels(self) -> None:
        class StockInfo:
            def __init__(self, symbol: str, quantity: str, available_quantity: str, cost_price: str) -> None:
                self.symbol = symbol
                self.quantity = quantity
                self.available_quantity = available_quantity
                self.cost_price = cost_price

        class Channel:
            stock_info = [
                StockInfo("700.HK", "650", "-450", "457.53"),
                StockInfo("AAPL.US", "-12", "-12", "180.00"),
            ]

        class Response:
            channels = [Channel()]

        class TradeContext:
            def stock_positions(self) -> Response:
                return Response()

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        positions = gw.get_positions()

        assert [p.symbol for p in positions] == ["700.HK", "AAPL.US"]
        assert positions[0].quantity == Decimal("650")
        assert positions[0].side == "LONG"
        assert positions[1].quantity == Decimal("12")
        assert positions[1].side == "SHORT"


class TestBrokerImports:
    def test_import_openapi_fallback(self) -> None:
        try:
            mod = _import_openapi()
            assert mod is not None
        except RuntimeError:
            pass


class TestBrokerDataclasses:
    def test_order_result(self) -> None:
        r = OrderResult(broker_order_id="123", symbol="AAPL.US", side="BUY", quantity=Decimal("10"), price=Decimal("150"), status="SUBMITTED")
        assert r.broker_order_id == "123"
        assert r.status == "SUBMITTED"

    def test_position(self) -> None:
        p = Position(symbol="AAPL.US", side="LONG", quantity=Decimal("10"), avg_price=Decimal("150"))
        assert p.symbol == "AAPL.US"
        assert p.side == "LONG"


class TestSideMap:
    def test_buy_maps_to_buy(self) -> None:
        assert _SIDE_MAP["BUY"] == "Buy"

    def test_sell_maps_to_sell(self) -> None:
        assert _SIDE_MAP["SELL"] == "Sell"

    def test_sell_short_maps_to_sell(self) -> None:
        assert _SIDE_MAP["SELL_SHORT"] == "Sell"

    def test_buy_to_cover_maps_to_buy(self) -> None:
        assert _SIDE_MAP["BUY_TO_COVER"] == "Buy"


class TestCashBalance:
    def test_cash_balance_fields(self) -> None:
        cb = CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("200"))
        assert cb.currency == "USD"
        assert cb.available_cash == Decimal("1000")
        assert cb.frozen_cash == Decimal("200")


class TestNetAsset:
    def test_net_asset_fields(self) -> None:
        na = NetAsset(currency="HKD", amount=Decimal("50000"))
        assert na.currency == "HKD"
        assert na.amount == Decimal("50000")


class TestAccountInfo:
    def test_account_info_with_primary_currency(self) -> None:
        cb = CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("200"))
        na = NetAsset(currency="USD", amount=Decimal("50000"))
        ai = AccountInfo(total_assets=Decimal("50000"), cash_balances=[cb], net_assets=[na])
        assert ai.total_assets == Decimal("50000")
        assert len(ai.cash_balances) == 1
        assert len(ai.net_assets) == 1

    def test_account_info_with_multiple_currencies(self) -> None:
        cb_usd = CashBalance(currency="USD", available_cash=Decimal("1000"), frozen_cash=Decimal("200"))
        cb_hkd = CashBalance(currency="HKD", available_cash=Decimal("5000"), frozen_cash=Decimal("100"))
        na_usd = NetAsset(currency="USD", amount=Decimal("50000"))
        na_hkd = NetAsset(currency="HKD", amount=Decimal("390000"))
        ai = AccountInfo(
            total_assets=Decimal("50000"),
            cash_balances=[cb_usd, cb_hkd],
            net_assets=[na_usd, na_hkd],
        )
        assert len(ai.cash_balances) == 2
        assert len(ai.net_assets) == 2


class TestGetAccount:
    def _make_fake_module(self):
        class BalanceItem:
            def __init__(self, currency, available_cash, frozen_cash, net_assets):
                self.currency = currency
                self.available_cash = available_cash
                self.frozen_cash = frozen_cash
                self.net_assets = net_assets

        class FakeConfig:
            @staticmethod
            def from_apikey(app_key, app_secret, access_token):
                return (app_key, app_secret, access_token)

        class FakeModule:
            Config = FakeConfig

            class QuoteContext:
                def __init__(self, config):
                    pass

            class TradeContext:
                def __init__(self, config):
                    self._config = config

        return FakeModule, BalanceItem

    def test_get_account_single_currency(self, monkeypatch) -> None:
        FakeModule, BalanceItem = self._make_fake_module()

        class TradeContext:
            def __init__(self, config):
                pass

            def account_balance(self):
                return [BalanceItem("USD", "5000", "1000", "50000")]

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        monkeypatch.setattr(broker_module.settings, "longbridge_app_key", "k", raising=False)
        monkeypatch.setattr(broker_module.settings, "longbridge_app_secret", "s", raising=False)
        monkeypatch.setattr(broker_module.settings, "longbridge_access_token", "t", raising=False)

        gw = BrokerGateway()
        gw._trade_ctx = TradeContext(None)
        gw._quote_ctx = object()
        result = gw.get_account()
        assert result.total_assets == Decimal("50000")
        assert len(result.cash_balances) == 1
        assert result.cash_balances[0].currency == "USD"
        assert result.cash_balances[0].available_cash == Decimal("5000")
        assert result.cash_balances[0].frozen_cash == Decimal("1000")
        assert len(result.net_assets) == 1
        assert result.net_assets[0].currency == "USD"
        assert result.net_assets[0].amount == Decimal("50000")

    def test_get_account_picks_primary_currency(self, monkeypatch) -> None:
        FakeModule, BalanceItem = self._make_fake_module()

        class TradeContext:
            def __init__(self, config):
                pass

            def account_balance(self):
                return [
                    BalanceItem("CNH", "1000", "200", "7200"),
                    BalanceItem("USD", "5000", "1000", "50000"),
                ]

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        monkeypatch.setattr(broker_module.settings, "longbridge_app_key", "k", raising=False)
        monkeypatch.setattr(broker_module.settings, "longbridge_app_secret", "s", raising=False)
        monkeypatch.setattr(broker_module.settings, "longbridge_access_token", "t", raising=False)

        gw = BrokerGateway()
        gw._trade_ctx = TradeContext(None)
        gw._quote_ctx = object()
        result = gw.get_account()
        assert result.total_assets == Decimal("50000")
        assert len(result.cash_balances) == 2
        assert len(result.net_assets) == 2

    def test_get_account_no_primary_currency(self, monkeypatch) -> None:
        FakeModule, BalanceItem = self._make_fake_module()

        class TradeContext:
            def __init__(self, config):
                pass

            def account_balance(self):
                return [BalanceItem("CNH", "1000", "200", "7200")]

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        monkeypatch.setattr(broker_module.settings, "longbridge_app_key", "k", raising=False)
        monkeypatch.setattr(broker_module.settings, "longbridge_app_secret", "s", raising=False)
        monkeypatch.setattr(broker_module.settings, "longbridge_access_token", "t", raising=False)

        gw = BrokerGateway()
        gw._trade_ctx = TradeContext(None)
        gw._quote_ctx = object()
        result = gw.get_account()
        assert result.total_assets == Decimal("7200")
        assert len(result.cash_balances) == 1
        assert result.cash_balances[0].currency == "CNH"

    def test_get_account_frozen_amounts_fallback(self, monkeypatch) -> None:
        FakeModule, BalanceItem = self._make_fake_module()

        class BalanceItemAlt:
            def __init__(self, currency, cash, frozen_amounts, net_assets):
                self.currency = currency
                self.cash = cash
                self.frozen_amounts = frozen_amounts
                self.net_assets = net_assets

        class TradeContext:
            def __init__(self, config):
                pass

            def account_balance(self):
                return [BalanceItemAlt("USD", "3000", "500", "40000")]

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        monkeypatch.setattr(broker_module.settings, "longbridge_app_key", "k", raising=False)
        monkeypatch.setattr(broker_module.settings, "longbridge_app_secret", "s", raising=False)
        monkeypatch.setattr(broker_module.settings, "longbridge_access_token", "t", raising=False)

        gw = BrokerGateway()
        gw._trade_ctx = TradeContext(None)
        gw._quote_ctx = object()
        result = gw.get_account()
        assert result.cash_balances[0].available_cash == Decimal("3000")
        assert result.cash_balances[0].frozen_cash == Decimal("500")
