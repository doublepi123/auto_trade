import json
from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import BarEvent, ControlEvent, EventSource, FillEvent, QuoteEvent


def test_quote_event_serializes_to_dict():
    event = QuoteEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        last_price=Decimal("150.25"),
        bid=Decimal("150.20"),
        ask=Decimal("150.30"),
        volume=1000,
    )
    data = event.to_dict()
    assert data["symbol"] == "AAPL.US"
    assert data["last_price"] == "150.25"
    assert data["event_type"] == "quote"


def test_bar_event_roundtrips_through_json():
    event = BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("150.00"),
        high=Decimal("151.00"),
        low=Decimal("149.50"),
        close=Decimal("150.50"),
        volume=5000,
    )
    data = event.to_dict()
    restored = BarEvent.from_dict(data)
    assert restored.close == Decimal("150.50")
    assert restored.symbol == "AAPL.US"


def test_fill_event_has_event_type():
    event = FillEvent(
        timestamp=datetime(2026, 6, 22, 10, 1, 0, tzinfo=timezone.utc),
        source=EventSource.BROKER,
        symbol="AAPL.US",
        broker_order_id="order-1",
        side="BUY",
        quantity=100,
        price=Decimal("150.25"),
        fee=Decimal("0.50"),
    )
    assert event.event_type == "fill"
