from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import BarEvent, EventSource
from app.platform.sdk import OrderIntent
from app.platform.simbroker import SimBroker


def test_simbroker_fills_limit_buy_when_price_drops():
    broker = SimBroker()
    intent = OrderIntent(
        symbol="AAPL.US",
        side="BUY",
        quantity=10,
        order_type="LIMIT",
        limit_price=Decimal("145"),
        reason="test",
    )
    order = broker.submit(intent)
    assert order.status == "SUBMITTED"

    bar = BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("146"),
        high=Decimal("146"),
        low=Decimal("144"),
        close=Decimal("144.5"),
        volume=100,
    )
    fills = broker.on_bar(bar)
    assert len(fills) == 1
    assert fills[0].side == "BUY"
    assert fills[0].quantity == 10
    assert fills[0].price == Decimal("145")


def test_simbroker_does_not_fill_when_price_not_reached():
    broker = SimBroker()
    intent = OrderIntent(
        symbol="AAPL.US",
        side="BUY",
        quantity=10,
        order_type="LIMIT",
        limit_price=Decimal("140"),
        reason="test",
    )
    broker.submit(intent)
    bar = BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("145"),
        high=Decimal("146"),
        low=Decimal("144"),
        close=Decimal("145.5"),
        volume=100,
    )
    fills = broker.on_bar(bar)
    assert len(fills) == 0
