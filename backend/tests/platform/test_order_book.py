"""Tests for the P194 L2 order book matching model."""

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import FillEvent
from app.platform.order_book import OrderBook, match_intent
from app.platform.sdk import OrderIntent


def _book(symbol: str = "AAPL.US") -> OrderBook:
    book = OrderBook(symbol=symbol, tick_size=Decimal("0.01"))
    book.apply_quote(
        bids=[(Decimal("100.00"), 200), (Decimal("99.90"), 300)],
        asks=[(Decimal("100.10"), 150), (Decimal("100.20"), 250)],
    )
    return book


def test_best_levels_and_spread():
    book = _book()
    assert book.best_bid() == Decimal("100.00")
    assert book.best_ask() == Decimal("100.10")
    assert book.spread() == Decimal("0.10")


def test_market_buy_walks_through_ask_levels():
    book = _book()
    intent = OrderIntent(symbol="AAPL.US", side="BUY", quantity=300, order_type="MARKET", reason="t")

    fills = book.match(intent, order_id="o1")

    assert len(fills) == 2
    assert fills[0].price == Decimal("100.10")
    assert fills[0].quantity == 150
    assert fills[1].price == Decimal("100.20")
    assert fills[1].quantity == 150
    assert sum(f.quantity for f in fills) == 300
    # Asks partially consumed: level 100.10 empty, 100.20 has 100 left.
    assert book.best_ask() == Decimal("100.20")


def test_market_sell_walks_through_bid_levels():
    book = _book()
    intent = OrderIntent(symbol="AAPL.US", side="SELL", quantity=400, order_type="MARKET", reason="t")

    fills = book.match(intent, order_id="o2")

    assert len(fills) == 2
    assert fills[0].price == Decimal("100.00")
    assert fills[0].quantity == 200
    assert fills[1].price == Decimal("99.90")
    assert fills[1].quantity == 200
    assert sum(f.quantity for f in fills) == 400


def test_market_buy_exhausts_liquidity_partially_filled():
    book = _book()
    intent = OrderIntent(symbol="AAPL.US", side="BUY", quantity=1000, order_type="MARKET", reason="t")

    fills = book.match(intent, order_id="o3")

    # Only 400 shares of ask liquidity available.
    assert sum(f.quantity for f in fills) == 400
    assert book.best_ask() is None


def test_limit_buy_rests_when_price_does_not_cross():
    book = _book()
    intent = OrderIntent(
        symbol="AAPL.US", side="BUY", quantity=100, order_type="LIMIT",
        limit_price=Decimal("99.50"), reason="t",
    )

    fills = book.match(intent, order_id="o4")

    assert fills == []
    depth = book.depth()
    # New resting bid at 99.50 appears below the existing levels.
    bid_prices = [p for p, _ in depth["bids"]]
    assert Decimal("99.50") in bid_prices


def test_limit_buy_crosses_and_sweeps():
    book = _book()
    intent = OrderIntent(
        symbol="AAPL.US", side="BUY", quantity=200, order_type="LIMIT",
        limit_price=Decimal("100.20"), reason="t",
    )

    fills = book.match(intent, order_id="o5")

    assert sum(f.quantity for f in fills) == 200
    # Should have taken 150 @ 100.10 then 50 @ 100.20.
    assert fills[0].price == Decimal("100.10")
    assert fills[0].quantity == 150
    assert fills[1].price == Decimal("100.20")
    assert fills[1].quantity == 50


def test_limit_crosses_then_rests_remainder():
    book = _book()
    intent = OrderIntent(
        symbol="AAPL.US", side="BUY", quantity=500, order_type="LIMIT",
        limit_price=Decimal("100.20"), reason="t",
    )

    fills = book.match(intent, order_id="o6")

    # 400 crossed (150 + 250), 100 rests at 100.20 on the bid side.
    assert sum(f.quantity for f in fills) == 400
    depth = book.depth()
    assert (Decimal("100.20"), 100) in depth["bids"]


def test_commission_applied_to_fills():
    book = _book()
    intent = OrderIntent(symbol="AAPL.US", side="BUY", quantity=150, order_type="MARKET", reason="t")

    fills = book.match(intent, order_id="o7", commission_rate=Decimal("0.001"))

    assert len(fills) == 1
    expected_commission = fills[0].price * Decimal(150) * Decimal("0.001")
    assert fills[0].commission == expected_commission


def test_fill_event_fields_are_populated():
    book = _book()
    intent = OrderIntent(symbol="AAPL.US", side="SELL", quantity=50, order_type="MARKET", reason="t")
    ts = datetime(2026, 6, 24, 10, tzinfo=timezone.utc)

    fills = book.match(intent, order_id="o8", clock=lambda: ts)

    assert len(fills) == 1
    fill = fills[0]
    assert isinstance(fill, FillEvent)
    assert fill.broker_order_id == "o8"
    assert fill.side == "SELL"
    assert fill.timestamp == ts
    assert fill.symbol == "AAPL.US"
    assert fill.price == Decimal("100.00")


def test_resting_limit_consumed_by_later_crossing():
    book = _book()
    # Rest a BUY at 99.50 (does not cross the 100.10 ask).
    book.match(
        OrderIntent(symbol="AAPL.US", side="BUY", quantity=100, order_type="LIMIT",
                    limit_price=Decimal("99.50"), reason="t"),
        order_id="resting1",
    )
    assert book.best_ask() == Decimal("100.10")

    # Quote moves down so 99.50 becomes best bid; a SELL market hits it.
    book.apply_quote(
        bids=[(Decimal("99.50"), 100)],
        asks=[(Decimal("99.55"), 50)],
    )
    sells = book.match(
        OrderIntent(symbol="AAPL.US", side="SELL", quantity=100, order_type="MARKET", reason="t"),
        order_id="taker1",
    )
    assert sum(f.quantity for f in sells) == 100


def test_depth_aggregation_and_levels_cap():
    book = OrderBook(symbol="X.US", tick_size=Decimal("0.01"))
    book.apply_quote(
        bids=[
            (Decimal("10.00"), 100),
            (Decimal("9.99"), 100),
            (Decimal("9.98"), 100),
            (Decimal("9.97"), 100),
        ],
        asks=[(Decimal("10.01"), 100)],
    )
    depth = book.depth(levels=2)
    assert len(depth["bids"]) == 2
    assert depth["bids"][0] == (Decimal("10.00"), 100)


def test_match_intent_functional_wrapper():
    book = _book()
    intent = OrderIntent(symbol="AAPL.US", side="BUY", quantity=50, order_type="MARKET", reason="t")

    fills = match_intent(book, intent, order_id="wrap1")

    assert sum(f.quantity for f in fills) == 50


def test_tick_size_quantization():
    book = OrderBook(symbol="X.US", tick_size=Decimal("0.05"))
    book.apply_quote(bids=[(Decimal("100.02"), 10)], asks=[(Decimal("100.07"), 10)])
    # 100.02 quantizes to 100.00; 100.07 quantizes to 100.05.
    assert book.best_bid() == Decimal("100.00")
    assert book.best_ask() == Decimal("100.05")
