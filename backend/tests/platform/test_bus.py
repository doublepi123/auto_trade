from datetime import datetime, timezone
from decimal import Decimal

from app.platform.bus import EventBus
from app.platform.events import BarEvent, EventSource, QuoteEvent


def test_bus_publishes_to_subscriber():
    bus = EventBus()
    received = []
    bus.subscribe("bar", lambda e: received.append(e))

    bar = BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("150"),
        high=Decimal("151"),
        low=Decimal("149"),
        close=Decimal("150.5"),
        volume=100,
    )
    bus.publish(bar)
    assert len(received) == 1
    assert received[0].close == Decimal("150.5")


def test_bus_filters_by_event_type():
    bus = EventBus()
    bars = []
    quotes = []
    bus.subscribe("bar", lambda e: bars.append(e))
    bus.subscribe("quote", lambda e: quotes.append(e))

    bus.publish(BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("150"),
        high=Decimal("151"),
        low=Decimal("149"),
        close=Decimal("150.5"),
        volume=100,
    ))
    bus.publish(QuoteEvent(
        timestamp=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        last_price=Decimal("150.6"),
    ))
    assert len(bars) == 1
    assert len(quotes) == 1
