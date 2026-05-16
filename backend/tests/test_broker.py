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

    def test_init_clients_uses_from_env(self, monkeypatch) -> None:
        called = {}

        class FakeConfig:
            @staticmethod
            def from_env():
                called["from_env"] = True
                return "fake-config"

        class FakeModule:
            Config = FakeConfig

            class QuoteContext:
                def __init__(self, config):
                    called["quote_ctx_config"] = config

            class TradeContext:
                def __init__(self, config):
                    called["trade_ctx_config"] = config

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)

        gw = BrokerGateway()
        gw._init_clients()

        assert called["from_env"] is True
        assert called["quote_ctx_config"] == "fake-config"
        assert called["trade_ctx_config"] == "fake-config"

    def test_init_clients_from_env_no_credentials(self, monkeypatch) -> None:
        called = {}

        class FakeConfig:
            @staticmethod
            def from_env():
                called["from_env"] = True
                return "fake-config"

        class FakeModule:
            Config = FakeConfig

            class QuoteContext:
                def __init__(self, config):
                    called["quote_ctx_config"] = config

            class TradeContext:
                def __init__(self, config):
                    called["trade_ctx_config"] = config

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)

        gw = BrokerGateway()
        gw._init_clients()

        assert called["from_env"] is True

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

    def test_subscribe_failure_does_not_mark_symbol_subscribed(self, monkeypatch) -> None:
        class FakeConfig:
            @staticmethod
            def from_env():
                return "fake-config"

        class QuoteContext:
            def __init__(self, _config):
                pass

            def set_on_quote(self, _callback):
                pass

            def subscribe(self, _symbols, _topics):
                raise RuntimeError("subscribe failed")

            def unsubscribe(self, _symbols):
                pass

        class TradeContext:
            def __init__(self, _config):
                pass

        class TopicType:
            Quote = "Quote"

        class FakeModule:
            pass

        FakeModule.Config = FakeConfig
        FakeModule.QuoteContext = QuoteContext
        FakeModule.TradeContext = TradeContext
        FakeModule.TopicType = TopicType

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        gw = BrokerGateway()

        try:
            gw.subscribe_quotes("AAPL.US", lambda _quote: None)
        except RuntimeError:
            pass

        assert gw._subscribed_symbol is None
        assert gw._quote_callbacks == []

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
        class FakeConfig:
            @staticmethod
            def from_env():
                return "fake-config"

        class FakeModule:
            Config = FakeConfig

            class QuoteContext:
                def __init__(self, config):
                    pass

            class TradeContext:
                def __init__(self, config):
                    self._config = config

        return FakeModule

    def test_get_account_single_currency(self, monkeypatch) -> None:
        FakeModule = self._make_fake_module()

        class CashInfo:
            def __init__(self, currency, available_cash, frozen_cash):
                self.currency = currency
                self.available_cash = available_cash
                self.frozen_cash = frozen_cash

        class BalanceItem:
            def __init__(self, currency, net_assets, cash_infos):
                self.currency = currency
                self.net_assets = net_assets
                self.cash_infos = cash_infos

        class TradeContext:
            def __init__(self, config):
                pass

            def account_balance(self):
                return [BalanceItem("USD", "50000", [CashInfo("USD", "5000", "1000")])]

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
        FakeModule = self._make_fake_module()

        class CashInfo:
            def __init__(self, currency, available_cash, frozen_cash):
                self.currency = currency
                self.available_cash = available_cash
                self.frozen_cash = frozen_cash

        class BalanceItem:
            def __init__(self, currency, net_assets, cash_infos):
                self.currency = currency
                self.net_assets = net_assets
                self.cash_infos = cash_infos

        class TradeContext:
            def __init__(self, config):
                pass

            def account_balance(self):
                return [
                    BalanceItem("CNH", "7200", [CashInfo("CNH", "1000", "200")]),
                    BalanceItem("USD", "50000", [CashInfo("USD", "5000", "1000")]),
                ]

        gw = BrokerGateway()
        gw._trade_ctx = TradeContext(None)
        gw._quote_ctx = object()
        result = gw.get_account()
        assert result.total_assets == Decimal("50000")
        assert len(result.cash_balances) == 2
        assert len(result.net_assets) == 2

    def test_get_account_no_primary_currency(self, monkeypatch) -> None:
        FakeModule = self._make_fake_module()

        class CashInfo:
            def __init__(self, currency, available_cash, frozen_cash):
                self.currency = currency
                self.available_cash = available_cash
                self.frozen_cash = frozen_cash

        class BalanceItem:
            def __init__(self, currency, net_assets, cash_infos):
                self.currency = currency
                self.net_assets = net_assets
                self.cash_infos = cash_infos

        class TradeContext:
            def __init__(self, config):
                pass

            def account_balance(self):
                return [BalanceItem("CNH", "7200", [CashInfo("CNH", "1000", "200")])]

        gw = BrokerGateway()
        gw._trade_ctx = TradeContext(None)
        gw._quote_ctx = object()
        result = gw.get_account()
        assert result.total_assets == Decimal("7200")
        assert len(result.cash_balances) == 1
        assert result.cash_balances[0].currency == "CNH"

    def test_get_account_multiple_cash_infos(self, monkeypatch) -> None:
        FakeModule = self._make_fake_module()

        class CashInfo:
            def __init__(self, currency, available_cash, frozen_cash):
                self.currency = currency
                self.available_cash = available_cash
                self.frozen_cash = frozen_cash

        class BalanceItem:
            def __init__(self, currency, net_assets, cash_infos):
                self.currency = currency
                self.net_assets = net_assets
                self.cash_infos = cash_infos

        class TradeContext:
            def __init__(self, config):
                pass

            def account_balance(self):
                return [BalanceItem("HKD", "200000", [CashInfo("USD", "1000", "200"), CashInfo("HKD", "5000", "100")])]

        gw = BrokerGateway()
        gw._trade_ctx = TradeContext(None)
        gw._quote_ctx = object()
        result = gw.get_account()
        assert result.total_assets == Decimal("200000")
        assert len(result.cash_balances) == 2
        assert result.cash_balances[0].currency == "USD"
        assert result.cash_balances[0].available_cash == Decimal("1000")
        assert result.cash_balances[1].currency == "HKD"
        assert result.cash_balances[1].available_cash == Decimal("5000")

    def test_get_cash_prefers_requested_currency(self) -> None:
        class CashInfo:
            def __init__(self, currency, available_cash):
                self.currency = currency
                self.available_cash = available_cash

        class BalanceItem:
            cash_infos = [CashInfo("USD", "1000"), CashInfo("HKD", "5000")]

        class TradeContext:
            def account_balance(self):
                return [BalanceItem()]

        gw = BrokerGateway()
        gw._trade_ctx = TradeContext()
        gw._quote_ctx = object()

        assert gw.get_cash("HKD") == Decimal("5000")
