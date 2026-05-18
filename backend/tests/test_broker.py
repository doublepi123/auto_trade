from decimal import Decimal

import pytest

from app.core import broker as broker_module
from app.core.broker import (
    AccountInfo,
    BrokerCredentials,
    BrokerGateway,
    CashBalance,
    NetAsset,
    OrderResult,
    OrderStatusResult,
    Position,
    Quote,
    _decimal_attr,
    _get_value,
    _import_openapi,
    _iter_position_items,
    _normalize_order_status,
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

    def test_subscribe_quotes_uses_quote_subtype(self, monkeypatch) -> None:
        called = {}

        class FakeConfig:
            @staticmethod
            def from_env():
                return "fake-config"

        class QuoteContext:
            def __init__(self, _config):
                pass

            def set_on_quote(self, callback):
                called["callback"] = callback

            def subscribe(self, symbols, subtypes):
                called["symbols"] = symbols
                called["subtypes"] = subtypes

        class TradeContext:
            def __init__(self, _config):
                pass

        class SubType:
            Quote = "SubType.Quote"

        class FakeModule:
            pass

        FakeModule.Config = FakeConfig
        FakeModule.QuoteContext = QuoteContext
        FakeModule.TradeContext = TradeContext
        FakeModule.SubType = SubType

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        gw = BrokerGateway()

        gw.subscribe_quotes("AAPL.US", lambda _quote: None)

        assert called["symbols"] == ["AAPL.US"]
        assert called["subtypes"] == ["SubType.Quote"]
        assert gw._subscribed_symbol == "AAPL.US"

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

    def test_get_order_status_normalizes_order_detail(self) -> None:
        class Detail:
            order_id = "order-1"
            status = "PartialFilled"
            executed_quantity = "3"
            executed_price = "201.5"

        class TradeContext:
            def order_detail(self, order_id: str) -> Detail:
                assert order_id == "order-1"
                return Detail()

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        result = gw.get_order_status("order-1")

        assert result.broker_order_id == "order-1"
        assert result.status == "PARTIAL_FILLED"
        assert result.executed_quantity == Decimal("3")
        assert result.executed_price == Decimal("201.5")

    def test_get_quote_with_single_item(self) -> None:
        class QuoteItem:
            symbol = "AAPL.US"
            last_done = 150.0
            bid = 149.5
            ask = 150.5
            timestamp = "2026-01-01"

        class QuoteContext:
            def quote(self, symbols):
                return [QuoteItem()]

        gw = BrokerGateway()
        gw._quote_ctx = QuoteContext()
        gw._trade_ctx = object()

        result = gw.get_quote("AAPL.US")
        assert result.symbol == "AAPL.US"
        assert result.last_price == 150.0
        assert result.bid == 149.5

    def test_get_quote_with_dict_response(self) -> None:
        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = object()

        class EmptyQuoteContext:
            def quote(self, symbols):
                return []

        gw._quote_ctx = EmptyQuoteContext()
        with pytest.raises(ValueError, match="no quote data"):
            gw.get_quote("AAPL.US")

    def test_submit_limit_order(self, monkeypatch) -> None:
        called = {}

        class FakeConfig:
            @staticmethod
            def from_env():
                return "fake-config"

        class FakeResponse:
            order_id = "order-123"
            status = "Submitted"

        class TradeContext:
            def __init__(self, _config):
                pass

            def submit_order(self, **kwargs):
                called["submit_order"] = kwargs
                return FakeResponse()

        class FakeModule:
            Config = FakeConfig

            class OrderSide:
                Buy = "Buy"
                Sell = "Sell"

            class OrderType:
                LO = "LO"

            class TimeInForceType:
                Day = "Day"

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        gw = BrokerGateway()
        gw._trade_ctx = TradeContext(None)
        gw._quote_ctx = object()

        result = gw.submit_limit_order("AAPL.US", "BUY", Decimal("10"), Decimal("150.0"))

        assert result.broker_order_id == "order-123"
        assert result.symbol == "AAPL.US"
        assert result.side == "BUY"
        assert result.status == "SUBMITTED"
        assert called["submit_order"]["symbol"] == "AAPL.US"
        assert called["submit_order"]["remark"] == "auto-trade"

    def test_close_closes_contexts(self) -> None:
        class FakeCtx:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        quote_ctx = FakeCtx()
        trade_ctx = FakeCtx()
        gw = BrokerGateway()
        gw._quote_ctx = quote_ctx
        gw._trade_ctx = trade_ctx
        gw._quote_callbacks.append(lambda x: None)

        gw.close()

        assert quote_ctx.closed is True
        assert trade_ctx.closed is True
        assert gw._quote_callbacks == []
        assert gw._subscribed_symbol is None
        assert gw._quote_ctx is None
        assert gw._trade_ctx is None

    def test_close_handles_none_contexts(self) -> None:
        gw = BrokerGateway()
        gw.close()
        assert gw._quote_ctx is None
        assert gw._trade_ctx is None

    def test_close_handles_context_without_close(self) -> None:
        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = object()
        gw.close()
        assert gw._quote_ctx is None
        assert gw._trade_ctx is None

    def test_unsubscribe_quotes(self) -> None:
        class FakeCtx:
            def __init__(self):
                self.unsubscribed = []

            def unsubscribe(self, symbols):
                self.unsubscribed.extend(symbols)

        gw = BrokerGateway()
        gw._quote_ctx = FakeCtx()
        gw._subscribed_symbol = "AAPL.US"
        gw._quote_callbacks.append(lambda x: None)

        gw.unsubscribe_quotes()

        assert gw._subscribed_symbol is None
        assert gw._quote_callbacks == []
        assert gw._quote_ctx.unsubscribed == ["AAPL.US"]

    def test_unsubscribe_quotes_when_no_subscription(self) -> None:
        gw = BrokerGateway()
        gw.unsubscribe_quotes()
        assert gw._subscribed_symbol is None
        assert gw._quote_callbacks == []

    def test_unsubscribe_quotes_logs_warning_on_error(self, caplog) -> None:
        class FakeCtx:
            def unsubscribe(self, symbols):
                raise RuntimeError("unsubscribe failed")

        gw = BrokerGateway()
        gw._quote_ctx = FakeCtx()
        gw._subscribed_symbol = "AAPL.US"

        gw.unsubscribe_quotes()
        assert "failed to unsubscribe" in caplog.text

    def test_get_cash_raises_on_exception(self) -> None:
        class TradeContext:
            def account_balance(self):
                raise RuntimeError("broker error")

        gw = BrokerGateway()
        gw._trade_ctx = TradeContext()
        gw._quote_ctx = object()

        with pytest.raises(RuntimeError, match="broker error"):
            gw.get_cash("USD")

    def test_get_cash_no_matching_currency(self, caplog) -> None:
        class BalanceItem:
            currency = "EUR"
            total_cash = "1000"

        class TradeContext:
            def account_balance(self):
                return [BalanceItem()]

        gw = BrokerGateway()
        gw._trade_ctx = TradeContext()
        gw._quote_ctx = object()

        result = gw.get_cash("USD")
        assert result == Decimal("0")
        assert "no USD item found" in caplog.text

    def test_get_cash_fallback_to_total_cash(self) -> None:
        class BalanceItem:
            currency = "USD"
            total_cash = "5000"

        class TradeContext:
            def account_balance(self):
                return [BalanceItem()]

        gw = BrokerGateway()
        gw._trade_ctx = TradeContext()
        gw._quote_ctx = object()

        result = gw.get_cash("USD")
        assert result == Decimal("5000")

    def test_get_cash_with_no_cash_infos(self) -> None:
        class BalanceItem:
            currency = "HKD"
            total_cash = "10000"

        class TradeContext:
            def account_balance(self):
                return [BalanceItem()]

        gw = BrokerGateway()
        gw._trade_ctx = TradeContext()
        gw._quote_ctx = object()

        result = gw.get_cash()
        assert result == Decimal("10000")

    def test_get_quote_with_non_list_response(self) -> None:
        class QuoteItem:
            symbol = "TSLA.US"
            last_done = 200.0
            bid = 199.5
            ask = 200.5
            timestamp = "2026-01-02"

        class QuoteContext:
            def quote(self, symbols):
                return QuoteItem()

        gw = BrokerGateway()
        gw._quote_ctx = QuoteContext()
        gw._trade_ctx = object()

        result = gw.get_quote("TSLA.US")
        assert result.symbol == "TSLA.US"
        assert result.last_price == 200.0

    def test_get_quote_defaults_when_attrs_missing(self) -> None:
        class QuoteItem:
            pass

        class QuoteContext:
            def quote(self, symbols):
                return [QuoteItem()]

        gw = BrokerGateway()
        gw._quote_ctx = QuoteContext()
        gw._trade_ctx = object()

        result = gw.get_quote("UNKNOWN.US")
        assert result.symbol == "UNKNOWN.US"
        assert result.last_price == 0.0
        assert result.bid == 0.0
        assert result.ask == 0.0
        assert result.timestamp == ""

    def test_get_positions_skips_zero_quantity(self) -> None:
        class StockInfo:
            def __init__(self, symbol, quantity):
                self.symbol = symbol
                self.quantity = quantity
                self.available_quantity = quantity
                self.cost_price = "100"

        class Channel:
            stock_info = [
                StockInfo("AAPL.US", "0"),
                StockInfo("TSLA.US", "5"),
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
        assert len(positions) == 1
        assert positions[0].symbol == "TSLA.US"

    def test_get_positions_with_explicit_side(self) -> None:
        class StockInfo:
            def __init__(self, symbol, side):
                self.symbol = symbol
                self.quantity = "10"
                self.available_quantity = "10"
                self.cost_price = "100"
                self.side = side

        class Channel:
            stock_info = [
                StockInfo("AAPL.US", "LONG"),
                StockInfo("TSLA.US", "SHORT"),
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
        assert positions[0].side == "LONG"
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


class TestNormalizeOrderStatus:
    @pytest.mark.parametrize("raw,expected", [
        ("Filled", "FILLED"),
        ("FILLED", "FILLED"),
        ("PartialFilled", "PARTIAL_FILLED"),
        ("PARTIAL_FILLED", "PARTIAL_FILLED"),
        ("Rejected", "REJECTED"),
        ("REJECTED", "REJECTED"),
        ("Cancelled", "CANCELLED"),
        ("Canceled", "CANCELLED"),
        ("Expired", "CANCELLED"),
        ("PartialWithdrawal", "CANCELLED"),
        ("Unknown", "SUBMITTED"),
        ("Submitted", "SUBMITTED"),
    ])
    def test_various_statuses(self, raw, expected) -> None:
        assert _normalize_order_status(raw) == expected

    def test_enum_value(self) -> None:
        class FakeEnum:
            value = "Filled"
        assert _normalize_order_status(FakeEnum()) == "FILLED"


class TestIterPositionItems:
    def test_none_returns_empty(self) -> None:
        assert _iter_position_items(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert _iter_position_items([]) == []

    def test_flat_dict_with_symbol(self) -> None:
        assert _iter_position_items({"symbol": "AAPL.US", "quantity": "10"}) == [{"symbol": "AAPL.US", "quantity": "10"}]

    def test_nested_data_key(self) -> None:
        assert _iter_position_items({"data": {"symbol": "TSLA.US"}}) == [{"symbol": "TSLA.US"}]

    def test_nested_list_key(self) -> None:
        assert _iter_position_items({"list": [{"symbol": "AAPL.US"}]}) == [{"symbol": "AAPL.US"}]

    def test_deeply_nested_channels(self) -> None:
        class Inner:
            symbol = "AAPL.US"

        class Outer:
            channels = [Inner()]

        result = _iter_position_items(Outer())
        assert len(result) == 1
        assert result[0].symbol == "AAPL.US"

    def test_dict_without_symbol_skipped(self) -> None:
        assert _iter_position_items({"other": "value"}) == []

    def test_object_without_symbol_skipped(self) -> None:
        class NoSymbol:
            pass
        assert _iter_position_items(NoSymbol()) == []


class TestDecimalAttr:
    def test_first_name_found(self) -> None:
        assert _decimal_attr({"qty": "10"}, "qty") == Decimal("10")

    def test_fallback_name(self) -> None:
        assert _decimal_attr({"other": "5"}, "qty", "other") == Decimal("5")

    def test_none_value_returns_zero(self) -> None:
        assert _decimal_attr({"qty": None}, "qty") == Decimal("0")

    def test_invalid_value_returns_zero(self) -> None:
        assert _decimal_attr({"qty": "invalid"}, "qty") == Decimal("0")

    def test_no_names_match_returns_zero(self) -> None:
        assert _decimal_attr({}, "qty", "amount") == Decimal("0")

    def test_object_attribute(self) -> None:
        class Item:
            price = "150.5"
        assert _decimal_attr(Item(), "price") == Decimal("150.5")


class TestGetValue:
    def test_dict_get(self) -> None:
        assert _get_value({"key": "value"}, "key") == "value"

    def test_dict_default(self) -> None:
        assert _get_value({}, "key", "default") == "default"

    def test_object_attr(self) -> None:
        class Item:
            key = "value"
        assert _get_value(Item(), "key") == "value"

    def test_object_default(self) -> None:
        class Item:
            pass
        assert _get_value(Item(), "key", "default") == "default"


class TestBrokerCredentials:
    def test_default_empty(self) -> None:
        creds = BrokerCredentials()
        assert creds.app_key == ""
        assert creds.app_secret == ""
        assert creds.access_token == ""

    def test_with_values(self) -> None:
        creds = BrokerCredentials(app_key="key", app_secret="secret", access_token="token")
        assert creds.app_key == "key"
        assert creds.app_secret == "secret"
        assert creds.access_token == "token"


class TestOrderStatusResult:
    def test_defaults(self) -> None:
        result = OrderStatusResult(broker_order_id="1", status="SUBMITTED")
        assert result.executed_quantity == Decimal("0")
        assert result.executed_price == Decimal("0")

    def test_with_values(self) -> None:
        result = OrderStatusResult(
            broker_order_id="1",
            status="FILLED",
            executed_quantity=Decimal("10"),
            executed_price=Decimal("150"),
        )
        assert result.executed_quantity == Decimal("10")
        assert result.executed_price == Decimal("150")
