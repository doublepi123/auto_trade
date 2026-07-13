# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from typing import Any
from decimal import Decimal

import pytest

from datetime import datetime, timezone

from app.core import broker as broker_module
from app.core.broker import (
    AccountInfo,
    BrokerCandle,
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
    _iter_order_items,
    _import_openapi,
    _iter_position_items,
    _normalize_order_status,
    _normalize_period_name,
    _parse_candle_timestamp,
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
    def test_get_order_status_includes_broker_charges_and_timestamps(self) -> None:
        class TradeContext:
            def order_detail(self, _order_id: str):
                return {
                    "order_id": "charged-order",
                    "status": "Filled",
                    "executed_quantity": "5",
                    "executed_price": "101.25",
                    "submitted_at": "2026-07-11T01:02:03Z",
                    "updated_at": "2026-07-11T01:02:05Z",
                    "charge_detail": {
                        "currency": "USD",
                        "total_amount": "1.23",
                    },
                }

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        result = gw.get_order_status("charged-order")

        assert result.actual_fee == Decimal("1.23")
        assert result.fee_currency == "USD"
        assert result.broker_submitted_at == datetime(2026, 7, 11, 1, 2, 3, tzinfo=timezone.utc)
        assert result.broker_updated_at == datetime(2026, 7, 11, 1, 2, 5, tzinfo=timezone.utc)

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

        assert gw._subscribed_symbols == set()
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
        assert gw._subscribed_symbols == {"AAPL.US"}

    def test_subscribe_quotes_merges_depth_bbo_into_quote_events(self, monkeypatch) -> None:
        called: dict[str, Any] = {}

        class FakeConfig:
            @staticmethod
            def from_env():
                return "fake-config"

        class QuoteContext:
            def __init__(self, _config):
                pass

            def set_on_quote(self, callback):
                called["quote_callback"] = callback

            def set_on_depth(self, callback):
                called["depth_callback"] = callback

            def subscribe(self, symbols, subtypes):
                called["symbols"] = symbols
                called["subtypes"] = subtypes

        class TradeContext:
            def __init__(self, _config):
                pass

        class SubType:
            Quote = "SubType.Quote"
            Depth = "SubType.Depth"

        class FakeModule:
            pass

        FakeModule.Config = FakeConfig
        FakeModule.QuoteContext = QuoteContext
        FakeModule.TradeContext = TradeContext
        FakeModule.SubType = SubType

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        received: list[Quote] = []
        gw = BrokerGateway()
        gw.subscribe_quotes("AAPL.US", received.append)

        class QuoteEvent:
            symbol = "AAPL.US"
            last_done = 150.05
            timestamp = "2026-07-13T17:00:00Z"

        class Level:
            def __init__(self, price: float) -> None:
                self.price = price

        class DepthEvent:
            symbol = "AAPL.US"
            bids = [Level(149.9), Level(150.0), Level(float("inf"))]
            asks = [Level(150.2), Level(150.1), Level(float("nan"))]

        called["quote_callback"]("AAPL.US", QuoteEvent())
        called["depth_callback"]("AAPL.US", DepthEvent())

        assert called["subtypes"] == ["SubType.Quote", "SubType.Depth"]
        assert received[-1] == Quote(
            symbol="AAPL.US",
            last_price=150.05,
            bid=150.0,
            ask=150.1,
            timestamp="2026-07-13T17:00:00Z",
        )

    def test_subscribe_quotes_adds_multiple_symbols_without_unsubscribing_previous(self, monkeypatch) -> None:
        called = {"subscribed": [], "unsubscribed": []}

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
                called["subscribed"].append((list(symbols), list(subtypes)))

            def unsubscribe(self, symbols):
                called["unsubscribed"].append(list(symbols))

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
        gw.subscribe_quotes("NVDA.US", lambda _quote: None)

        assert gw._subscribed_symbols == {"AAPL.US", "NVDA.US"}
        assert called["unsubscribed"] == []
        assert called["subscribed"] == [
            (["AAPL.US"], ["SubType.Quote"]),
            (["NVDA.US"], ["SubType.Quote"]),
        ]

    def test_get_positions_flattens_stock_position_channels(self) -> None:
        class StockInfo:
            def __init__(self, symbol: str, quantity: str, available_quantity: str, cost_price: str) -> None:
                self.symbol = symbol
                self.quantity = quantity
                self.available_quantity = available_quantity
                self.cost_price = cost_price

        class Channel:
            stock_info = [
                StockInfo("700.HK", "650", "450", "457.53"),
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

    def test_cancelled_order_does_not_treat_submitted_fields_as_fill(self) -> None:
        class Detail:
            order_id = "order-cancelled"
            status = "Cancelled"
            quantity = "10"
            price = "100"

        class TradeContext:
            def order_detail(self, _order_id: str) -> Detail:
                return Detail()

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        result = gw.get_order_status("order-cancelled")

        assert result.status == "CANCELLED"
        assert result.executed_quantity == Decimal("0")
        assert result.executed_price == Decimal("0")

    def test_get_today_orders_normalizes_trade_context_orders(self) -> None:
        class Detail:
            def __init__(self, order_id: str, symbol: str) -> None:
                self.order_id = order_id
                self.symbol = symbol
                self.side = "Sell"
                self.submitted_quantity = "7"
                self.submitted_price = "225.5"
                self.executed_quantity = "2"
                self.executed_price = "225.6"
                self.status = "PartialFilled"
                self.created_at = "2026-05-22T13:00:00Z"

        class TradeContext:
            def today_orders(self):
                return [Detail("order-1", "NVDA.US")]

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        orders = gw.get_today_orders()

        assert len(orders) == 1
        assert orders[0].broker_order_id == "order-1"
        assert orders[0].symbol == "NVDA.US"
        assert orders[0].side == "SELL"
        assert orders[0].quantity == Decimal("7")
        assert orders[0].price == Decimal("225.5")
        assert orders[0].executed_quantity == Decimal("2")
        assert orders[0].status == "PARTIAL_FILLED"

    def test_get_today_orders_rejects_order_without_id(self) -> None:
        class TradeContext:
            def today_orders(self):
                return [
                    {
                        "symbol": "NVDA.US",
                        "side": "Sell",
                        "status": "Filled",
                        "submitted_quantity": "5",
                        "submitted_price": "100",
                        "executed_quantity": "5",
                    }
                ]

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        with pytest.raises(ValueError, match="without broker_order_id"):
            gw.get_today_orders()

    @pytest.mark.parametrize("value", ["invalid", "nan", "inf", "-1"])
    def test_get_today_orders_rejects_invalid_executed_quantity(
        self,
        value: str,
    ) -> None:
        class TradeContext:
            def today_orders(self):
                return [
                    {
                        "order_id": "bad-execution",
                        "symbol": "NVDA.US",
                        "side": "Sell",
                        "status": "Cancelled",
                        "submitted_quantity": "5",
                        "submitted_price": "100",
                        "executed_quantity": value,
                    }
                ]

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        with pytest.raises(ValueError, match="invalid executed_quantity"):
            gw.get_today_orders()

    def test_get_order_status_rejects_invalid_execution_fields(self) -> None:
        class TradeContext:
            def order_detail(self, _order_id: str):
                return {
                    "order_id": "bad-detail",
                    "status": "Filled",
                    "executed_quantity": "5",
                    "executed_price": "nan",
                }

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        with pytest.raises(ValueError, match="invalid executed_price"):
            gw.get_order_status("bad-detail")

    @pytest.mark.parametrize("response_id", [None, "", "another-order"])
    def test_get_order_status_rejects_missing_or_mismatched_id(
        self,
        response_id: str | None,
    ) -> None:
        class TradeContext:
            def order_detail(self, _order_id: str):
                return {
                    "order_id": response_id,
                    "status": "Filled",
                    "executed_quantity": "5",
                    "executed_price": "100",
                }

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        with pytest.raises(ValueError, match="missing order_id|id mismatch"):
            gw.get_order_status("expected-order")

    def test_cancel_order_rejects_mismatched_detail_id(self) -> None:
        class TradeContext:
            def cancel_order(self, _order_id: str):
                return None

            def order_detail(self, _order_id: str):
                return {
                    "order_id": "another-order",
                    "status": "Cancelled",
                }

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        with pytest.raises(ValueError, match="id mismatch"):
            gw.cancel_order("expected-order")

    def test_today_order_updated_at_is_only_used_after_execution(self) -> None:
        updated_at = "2026-05-22T13:05:00Z"

        class Detail:
            def __init__(self, order_id: str, status: str, executed: str) -> None:
                self.order_id = order_id
                self.symbol = "NVDA.US"
                self.side = "Buy"
                self.submitted_quantity = "7"
                self.submitted_price = "225.5"
                self.executed_quantity = executed
                self.executed_price = "225.6" if executed != "0" else "0"
                self.status = status
                self.created_at = "2026-05-22T13:00:00Z"
                self.updated_at = updated_at

        class TradeContext:
            def today_orders(self):
                return [
                    Detail("pending", "Submitted", "0"),
                    Detail("filled", "Filled", "7"),
                ]

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        orders = gw.get_today_orders()

        assert orders[0].filled_at is None
        assert orders[1].filled_at == datetime(2026, 5, 22, 13, 5, tzinfo=timezone.utc)

    def test_cancel_order_uses_trade_context_cancel_order(self) -> None:
        called = {}

        class Response:
            order_id = "order-1"
            status = "Canceled"

        class TradeContext:
            def cancel_order(self, order_id: str):
                called["order_id"] = order_id
                return None

            def order_detail(self, order_id: str):
                assert order_id == "order-1"
                return Response()

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        result = gw.cancel_order("order-1")

        assert called["order_id"] == "order-1"
        assert result.broker_order_id == "order-1"
        assert result.status == "CANCELLED"

    def test_cancel_order_falls_back_to_withdraw_order(self) -> None:
        called = {}

        class Response:
            order_id = "order-2"
            status = "Cancelled"

        class TradeContext:
            def withdraw_order(self, order_id: str):
                called["order_id"] = order_id
                return None

            def order_detail(self, order_id: str):
                assert order_id == "order-2"
                return Response()

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        result = gw.cancel_order("order-2")

        assert called["order_id"] == "order-2"
        assert result.status == "CANCELLED"

    def test_cancel_order_without_terminal_confirmation_remains_live(self) -> None:
        class TradeContext:
            def cancel_order(self, _order_id: str):
                return None

            def order_detail(self, _order_id: str):
                raise TimeoutError("status unavailable")

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        result = gw.cancel_order("order-3")

        assert result.broker_order_id == "order-3"
        assert result.status == "SUBMITTED"
        assert result.executed_quantity == Decimal("0")

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

    def test_get_quote_uses_depth_when_quote_has_no_bbo(self) -> None:
        class QuoteItem:
            symbol = "AAPL.US"
            last_done = 150.05
            timestamp = "2026-07-13T17:00:00Z"

        class Level:
            def __init__(self, price: float) -> None:
                self.price = price

        class Depth:
            bids = [Level(149.9), Level(150.0)]
            asks = [Level(150.2), Level(150.1)]

        class QuoteContext:
            def quote(self, _symbols):
                return [QuoteItem()]

            def depth(self, symbol):
                assert symbol == "AAPL.US"
                return Depth()

        gw = BrokerGateway()
        gw._quote_ctx = QuoteContext()
        gw._trade_ctx = object()

        result = gw.get_quote("AAPL.US")

        assert result.bid == 150.0
        assert result.ask == 150.1

    def test_get_quote_with_dict_response(self) -> None:
        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = object()

        class EmptyQuoteContext:
            def quote(self, symbols):
                return []

        gw._quote_ctx = EmptyQuoteContext()
        with pytest.raises(RuntimeError, match="broker returned 0 quotes for 1 symbols"):
            gw.get_quote("AAPL.US")

    def test_get_candlesticks_returns_normalized_bars(self, monkeypatch) -> None:
        class FakeAdjust:
            NoAdjust = "noadj"

        class FakePeriod:
            Day = "DAY"

        class FakeCandle:
            def __init__(self, ts, o, h, l, c, v):
                self.timestamp = ts
                self.open = o
                self.high = h
                self.low = l
                self.close = c
                self.volume = v
                self.turnover = v * c

        class FakeQuoteContext:
            def __init__(self) -> None:
                self.calls: list[tuple[Any, ...]] = []

            def candlesticks(self, symbol, period, count, adjust):
                self.calls.append((symbol, period, count, adjust))
                return [
                    FakeCandle(datetime(2026, 5, 2, tzinfo=timezone.utc), 100, 105, 99, 103, 1500),
                    FakeCandle(datetime(2026, 5, 1, tzinfo=timezone.utc), 99, 102, 97, 100, 1200),
                ]

        class FakeModule:
            Period = FakePeriod
            AdjustType = FakeAdjust

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        gw = BrokerGateway()
        gw._quote_ctx = FakeQuoteContext()
        gw._trade_ctx = object()

        candles = gw.get_candlesticks("AAPL.US", "Day", 2)

        assert len(candles) == 2
        assert candles[0].timestamp < candles[1].timestamp, "result must be sorted ascending"
        assert all(isinstance(c, BrokerCandle) for c in candles)
        assert candles[1].close == 103.0
        assert gw._quote_ctx.calls[0] == ("AAPL.US", "DAY", 2, "noadj")

    def test_get_candlesticks_drops_invalid_upstream_ohlcv(self, monkeypatch, caplog) -> None:
        class FakeAdjust:
            NoAdjust = "NO_ADJUST"

        class FakePeriod:
            Min_1 = "MIN_1"

        class FakeModule:
            Period = FakePeriod
            AdjustType = FakeAdjust

        class Candle:
            def __init__(self, minute: int, volume: float) -> None:
                self.timestamp = datetime(2026, 7, 13, 13, minute, tzinfo=timezone.utc)
                self.open = 100
                self.high = 101
                self.low = 99
                self.close = 100.5
                self.volume = volume
                self.turnover = 1000

        class QuoteContext:
            def candlesticks(self, _symbol, _period, _count, _adjust):
                return [Candle(30, -1), Candle(31, 100)]

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        gw = BrokerGateway()
        gw._quote_ctx = QuoteContext()
        gw._trade_ctx = object()

        result = gw.get_candlesticks("AAPL.US", "MIN_1", 2)

        assert [item.volume for item in result] == [100]
        assert "dropped 1 invalid MIN_1 candlesticks for AAPL.US" in caplog.text

    def test_get_candlesticks_handles_zero_count(self) -> None:
        gw = BrokerGateway()
        gw._quote_ctx = object()
        assert gw.get_candlesticks("AAPL.US", "Day", 0) == []

    def test_get_candlesticks_rejects_unknown_period(self, monkeypatch) -> None:
        class FakeModule:
            class Period:
                Day = "DAY"

            class AdjustType:
                NoAdjust = "noadj"

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = object()
        with pytest.raises(ValueError, match="unsupported candlestick period"):
            gw.get_candlesticks("AAPL.US", "Tick_1", 1)

    def test_get_candlesticks_raises_when_no_adjust_missing(self, monkeypatch) -> None:
        """B4: getattr(AdjustType, \"NoAdjust\", None) must raise RuntimeError when NoAdjust is absent."""
        class FakeModule:
            class Period:
                Day = "DAY"

            class AdjustType:
                pass  # No NoAdjust attribute

        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = object()
        with pytest.raises(RuntimeError, match="AdjustType.NoAdjust not found in SDK"):
            gw.get_candlesticks("AAPL.US", "Day", 1)

    def test_normalize_period_name(self) -> None:
        assert _normalize_period_name("Day") == "Day"
        assert _normalize_period_name("MIN_1") == "Min_1"
        assert _normalize_period_name("MIN5") == "Min_5"
        assert _normalize_period_name("1D") == "Day"
        assert _normalize_period_name("WEEK") == "Week"

    def test_parse_candle_timestamp_from_datetime(self) -> None:
        naive = datetime(2026, 5, 1, 12, 0, 0)
        parsed = _parse_candle_timestamp(naive)
        assert parsed is not None and parsed.tzinfo is timezone.utc

    def test_parse_candle_timestamp_from_unix_seconds(self) -> None:
        parsed = _parse_candle_timestamp(1714563600)
        assert parsed is not None and parsed.tzinfo is timezone.utc

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
        assert gw._subscribed_symbols == set()
        assert gw._quote_ctx is None
        assert gw._trade_ctx is None
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
        gw._subscribed_symbols = {"AAPL.US", "NVDA.US"}
        gw._quote_callbacks.append(lambda x: None)

        gw.unsubscribe_quotes()

        assert gw._subscribed_symbols == set()
        assert gw._quote_callbacks == []
        assert gw._quote_ctx.unsubscribed == ["AAPL.US", "NVDA.US"]

    def test_unsubscribe_quotes_when_no_subscription(self) -> None:
        gw = BrokerGateway()
        gw.unsubscribe_quotes()
        assert gw._subscribed_symbols == set()
        assert gw._quote_callbacks == []

    def test_unsubscribe_quotes_logs_warning_on_error(self, caplog) -> None:
        class FakeCtx:
            def unsubscribe(self, symbols):
                raise RuntimeError("unsubscribe failed")

        gw = BrokerGateway()
        gw._quote_ctx = FakeCtx()
        gw._subscribed_symbols = {"AAPL.US"}

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

    def test_estimate_margin_max_quantity_uses_longbridge_estimate(self) -> None:
        called = {}

        class Response:
            cash_max_qty = Decimal("12")
            margin_max_qty = Decimal("45")

        class TradeContext:
            def estimate_max_purchase_quantity(self, **kwargs):
                called.update(kwargs)
                return Response()

        class OrderSide:
            Buy = "OrderSide.Buy"
            Sell = "OrderSide.Sell"

        class OrderType:
            LO = "OrderType.LO"

        class FakeModule:
            pass

        FakeModule.OrderSide = OrderSide
        FakeModule.OrderType = OrderType

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)
        try:
            qty = gw.estimate_margin_max_quantity("NVDA.US", "BUY", Decimal("222.50"), "USD")
        finally:
            monkeypatch.undo()

        assert qty == Decimal("45")
        assert called == {
            "symbol": "NVDA.US",
            "order_type": "OrderType.LO",
            "side": "OrderSide.Buy",
            "price": Decimal("222.50"),
            "currency": "USD",
            "fractional_shares": False,
        }

    def test_estimate_margin_max_quantity_supports_sell_side(self, monkeypatch) -> None:
        called = {}

        class Response:
            margin_max_qty = "88"

        class TradeContext:
            def estimate_max_purchase_quantity(self, **kwargs):
                called.update(kwargs)
                return Response()

        class OrderSide:
            Buy = "OrderSide.Buy"
            Sell = "OrderSide.Sell"

        class OrderType:
            LO = "OrderType.LO"

        class FakeModule:
            pass

        FakeModule.OrderSide = OrderSide
        FakeModule.OrderType = OrderType
        monkeypatch.setattr(broker_module, "_import_openapi", lambda: FakeModule)

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        qty = gw.estimate_margin_max_quantity("NVDA.US", "SELL", Decimal("225.00"), "USD")

        assert qty == Decimal("88")
        assert called["side"] == "OrderSide.Sell"

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

    @pytest.mark.parametrize("quantity", ["invalid", "nan", "inf"])
    def test_get_positions_rejects_invalid_quantity(self, quantity: str) -> None:
        class TradeContext:
            def stock_positions(self):
                return [
                    {
                        "symbol": "AAPL.US",
                        "quantity": quantity,
                        "available_quantity": "1",
                        "cost_price": "100",
                    }
                ]

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        with pytest.raises(ValueError, match="invalid quantity"):
            gw.get_positions()

    @pytest.mark.parametrize("available", ["invalid", "nan", "inf", "11"])
    def test_get_positions_rejects_invalid_available_quantity(
        self,
        available: str,
    ) -> None:
        class TradeContext:
            def stock_positions(self):
                return [
                    {
                        "symbol": "AAPL.US",
                        "quantity": "10",
                        "available_quantity": available,
                        "cost_price": "100",
                    }
                ]

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        with pytest.raises(ValueError, match="available quantity"):
            gw.get_positions()

    def test_get_positions_rejects_missing_symbol(self) -> None:
        class TradeContext:
            def stock_positions(self):
                return [{"quantity": "1", "cost_price": "100"}]

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        with pytest.raises(ValueError, match="without symbol"):
            gw.get_positions()

    def test_get_positions_rejects_invalid_present_average_price(self) -> None:
        class TradeContext:
            def stock_positions(self):
                return [
                    {
                        "symbol": "AAPL.US",
                        "quantity": "1",
                        "available_quantity": "1",
                        "cost_price": "0",
                    }
                ]

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        with pytest.raises(ValueError, match="invalid average price"):
            gw.get_positions()

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

    def test_get_positions_rejects_unknown_explicit_side(self) -> None:
        class TradeContext:
            def stock_positions(self):
                return [
                    {
                        "symbol": "AAPL.US",
                        "side": "UNKNOWN",
                        "quantity": "10",
                        "available_quantity": "10",
                        "cost_price": "100",
                    }
                ]

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        with pytest.raises(ValueError, match="invalid side"):
            gw.get_positions()

    def test_get_positions_rejects_conflicting_quantity_signs(self) -> None:
        class TradeContext:
            def stock_positions(self):
                return [
                    {
                        "symbol": "AAPL.US",
                        "quantity": "10",
                        "available_quantity": "-10",
                        "cost_price": "100",
                    }
                ]

        gw = BrokerGateway()
        gw._quote_ctx = object()
        gw._trade_ctx = TradeContext()

        with pytest.raises(ValueError, match="conflicting signs"):
            gw.get_positions()


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

    def test_dict_without_position_schema_rejected(self) -> None:
        with pytest.raises(ValueError, match="unrecognized position response"):
            _iter_position_items({"other": "value"})

    def test_object_without_position_schema_rejected(self) -> None:
        class NoSymbol:
            pass

        with pytest.raises(ValueError, match="unrecognized position response"):
            _iter_position_items(NoSymbol())

    @pytest.mark.parametrize(
        "payload",
        [{"data": None}, {"positions": None}, {"channels": None}],
    )
    def test_null_position_container_is_rejected(self, payload) -> None:
        with pytest.raises(ValueError, match="null position"):
            _iter_position_items(payload)


class TestIterOrderItems:
    def test_unknown_nonempty_order_response_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unrecognized order response"):
            _iter_order_items({"unexpected": "value"})

    @pytest.mark.parametrize(
        "payload",
        [{"data": None}, {"orders": None}, {"items": None}],
    )
    def test_null_order_container_is_rejected(self, payload) -> None:
        with pytest.raises(ValueError, match="null order"):
            _iter_order_items(payload)


class TestDecimalAttr:
    def test_does_not_catch_keyboard_interrupt(self) -> None:
        """B3: _decimal_attr must not swallow KeyboardInterrupt."""
        class RaisesOnStr:
            def __str__(self) -> str:
                raise KeyboardInterrupt()

        class Item:
            qty = RaisesOnStr()

        with pytest.raises(KeyboardInterrupt):
            _decimal_attr(Item(), "qty")

    def test_does_not_catch_system_exit(self) -> None:
        """B3: _decimal_attr must not swallow SystemExit."""
        class RaisesOnStr:
            def __str__(self) -> str:
                raise SystemExit(1)

        class Item:
            qty = RaisesOnStr()

        with pytest.raises(SystemExit):
            _decimal_attr(Item(), "qty")

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
