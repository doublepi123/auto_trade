from decimal import Decimal

from app.core.broker import BrokerGateway, OrderResult, Position, Quote, _import_openapi


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
