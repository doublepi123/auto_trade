import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from app.platform.events import (
    BarEvent,
    ControlEvent,
    EVENT_REGISTRY,
    EventSource,
    FillEvent,
    OrderEvent,
    OrderIntentEvent,
    QuoteEvent,
    RiskEvent,
    SignalEvent,
    event_from_dict,
)


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
    assert data["source"] == "market"
    assert isinstance(data["event_id"], str)
    assert isinstance(data["timestamp"], str)


def test_quote_event_roundtrips_through_json():
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
    restored = QuoteEvent.from_dict(data)
    assert restored.last_price == Decimal("150.25")
    assert restored.bid == Decimal("150.20")
    assert restored.ask == Decimal("150.30")
    assert restored.volume == 1000
    assert restored.symbol == "AAPL.US"
    assert restored.source == EventSource.MARKET


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
    assert restored.open == Decimal("150.00")
    assert restored.high == Decimal("151.00")
    assert restored.low == Decimal("149.50")
    assert restored.volume == 5000


def test_signal_event_roundtrips_with_params():
    event = SignalEvent(
        timestamp=datetime(2026, 6, 22, 10, 5, 0, tzinfo=timezone.utc),
        source=EventSource.STRATEGY,
        symbol="AAPL.US",
        signal_type="buy_low",
        side="BUY",
        price=Decimal("149.00"),
        quantity=10,
        reason="price_below_threshold",
        params={"threshold": "150.00"},
    )
    data = event.to_dict()
    restored = SignalEvent.from_dict(data)
    assert restored.signal_type == "buy_low"
    assert restored.side == "BUY"
    assert restored.price == Decimal("149.00")
    assert restored.quantity == 10
    assert restored.reason == "price_below_threshold"
    assert restored.params == {"threshold": "150.00"}
    assert restored.symbol == "AAPL.US"
    assert restored.source == EventSource.STRATEGY


def test_signal_event_defaults_params_to_empty_dict():
    event = SignalEvent(
        timestamp=datetime(2026, 6, 22, 10, 5, 0, tzinfo=timezone.utc),
        source=EventSource.STRATEGY,
        symbol="AAPL.US",
        signal_type="sell_high",
        reason="price_above_threshold",
    )
    assert event.params == {}
    data = event.to_dict()
    restored = SignalEvent.from_dict(data)
    assert restored.params == {}


def test_signal_event_accepts_none_params():
    event = SignalEvent(
        timestamp=datetime(2026, 6, 22, 10, 5, 0, tzinfo=timezone.utc),
        source=EventSource.STRATEGY,
        symbol="AAPL.US",
        signal_type="sell_high",
        reason="price_above_threshold",
        params=None,
    )
    assert event.params == {}


def test_order_intent_event_roundtrips():
    event = OrderIntentEvent(
        timestamp=datetime(2026, 6, 22, 10, 10, 0, tzinfo=timezone.utc),
        source=EventSource.STRATEGY,
        symbol="AAPL.US",
        side="BUY",
        quantity=100,
        order_type="LIMIT",
        limit_price=Decimal("149.50"),
        reason="signal_triggered",
    )
    data = event.to_dict()
    restored = OrderIntentEvent.from_dict(data)
    assert restored.side == "BUY"
    assert restored.quantity == 100
    assert restored.order_type == "LIMIT"
    assert restored.limit_price == Decimal("149.50")
    assert restored.reason == "signal_triggered"
    assert restored.symbol == "AAPL.US"


def test_order_event_roundtrips():
    event = OrderEvent(
        timestamp=datetime(2026, 6, 22, 10, 15, 0, tzinfo=timezone.utc),
        source=EventSource.BROKER,
        symbol="AAPL.US",
        broker_order_id="bo-123",
        status="SUBMITTED",
        filled_quantity=0,
        avg_price=None,
    )
    data = event.to_dict()
    restored = OrderEvent.from_dict(data)
    assert restored.broker_order_id == "bo-123"
    assert restored.status == "SUBMITTED"
    assert restored.filled_quantity == 0
    assert restored.avg_price is None
    assert restored.symbol == "AAPL.US"
    assert restored.source == EventSource.BROKER


def test_fill_event_full_roundtrip():
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
    data = event.to_dict()
    restored = FillEvent.from_dict(data)
    assert restored.broker_order_id == "order-1"
    assert restored.side == "BUY"
    assert restored.quantity == 100
    assert restored.price == Decimal("150.25")
    assert restored.fee == Decimal("0.50")
    assert restored.symbol == "AAPL.US"
    assert restored.source == EventSource.BROKER


def test_fill_event_default_fee():
    event = FillEvent(
        timestamp=datetime(2026, 6, 22, 10, 1, 0, tzinfo=timezone.utc),
        source=EventSource.BROKER,
        symbol="AAPL.US",
        broker_order_id="order-2",
        side="SELL",
        quantity=50,
        price=Decimal("151.00"),
    )
    assert event.fee == Decimal("0")
    data = event.to_dict()
    restored = FillEvent.from_dict(data)
    assert restored.fee == Decimal("0")


def test_risk_event_roundtrips():
    event = RiskEvent(
        timestamp=datetime(2026, 6, 22, 10, 20, 0, tzinfo=timezone.utc),
        source=EventSource.RISK,
        symbol="AAPL.US",
        risk_type="daily_loss_limit",
        severity="WARNING",
        message="Daily loss exceeded $100",
    )
    data = event.to_dict()
    restored = RiskEvent.from_dict(data)
    assert restored.risk_type == "daily_loss_limit"
    assert restored.severity == "WARNING"
    assert restored.message == "Daily loss exceeded $100"
    assert restored.symbol == "AAPL.US"
    assert restored.source == EventSource.RISK


def test_control_event_roundtrips():
    event = ControlEvent(
        timestamp=datetime(2026, 6, 22, 10, 25, 0, tzinfo=timezone.utc),
        source=EventSource.SYSTEM,
        action="pause",
    )
    data = event.to_dict()
    restored = ControlEvent.from_dict(data)
    assert restored.action == "pause"
    assert restored.source == EventSource.SYSTEM
    assert restored.symbol is None


def test_event_from_dict_factory_with_quote_event():
    event = QuoteEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="TSLA.US",
        last_price=Decimal("200.00"),
        volume=500,
    )
    data = event.to_dict()
    restored = event_from_dict(data)
    assert isinstance(restored, QuoteEvent)
    assert restored.last_price == Decimal("200.00")
    assert restored.symbol == "TSLA.US"


def test_event_from_dict_factory_with_bar_event():
    event = BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="TSLA.US",
        open=Decimal("199.00"),
        high=Decimal("201.00"),
        low=Decimal("198.50"),
        close=Decimal("200.00"),
        volume=1000,
    )
    data = event.to_dict()
    restored = event_from_dict(data)
    assert isinstance(restored, BarEvent)
    assert restored.close == Decimal("200.00")
    assert restored.symbol == "TSLA.US"


def test_event_from_dict_unknown_event_type_raises():
    try:
        event_from_dict({"event_type": "unknown"})
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "Unknown event_type" in str(e)


def test_event_registry_contains_all_event_types():
    expected = {
        "quote",
        "bar",
        "signal",
        "order_intent",
        "order",
        "fill",
        "risk",
        "control",
        "regime",
    }
    assert set(EVENT_REGISTRY.keys()) == expected
    assert len(EVENT_REGISTRY) == 9


def test_to_dict_converts_event_id_to_string():
    event = ControlEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc),
        source=EventSource.SYSTEM,
        action="start",
    )
    data = event.to_dict()
    assert isinstance(data["event_id"], str)
    assert UUID(data["event_id"])


def test_to_dict_converts_source_to_string():
    event = ControlEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc),
        source=EventSource.SYSTEM,
        action="start",
    )
    data = event.to_dict()
    assert data["source"] == "system"
    assert isinstance(data["source"], str)


def test_to_dict_converts_timestamp_to_isoformat():
    ts = datetime(2026, 6, 22, 10, 0, 0, tzinfo=timezone.utc)
    event = ControlEvent(
        timestamp=ts,
        source=EventSource.SYSTEM,
        action="start",
    )
    data = event.to_dict()
    assert data["timestamp"] == "2026-06-22T10:00:00+00:00"
    assert isinstance(data["timestamp"], str)


def test_full_json_serialization_roundtrip():
    """Test that events can be serialized to JSON and back via the factory."""
    event = SignalEvent(
        timestamp=datetime(2026, 6, 22, 10, 5, 0, tzinfo=timezone.utc),
        source=EventSource.STRATEGY,
        symbol="AAPL.US",
        signal_type="buy_low",
        side="BUY",
        price=Decimal("149.00"),
        quantity=10,
        reason="price_below_threshold",
        params={"threshold": "150.00"},
    )
    json_str = json.dumps(event.to_dict())
    data = json.loads(json_str)
    restored = event_from_dict(data)
    assert isinstance(restored, SignalEvent)
    assert restored.signal_type == "buy_low"
    assert restored.price == Decimal("149.00")
    assert restored.params == {"threshold": "150.00"}


def test_fill_event_roundtrips_with_slippage_commission_and_partial():
    event = FillEvent(
        timestamp=datetime(2026, 6, 22, 10, 1, 0, tzinfo=timezone.utc),
        source=EventSource.BROKER,
        symbol="AAPL.US",
        broker_order_id="order-3",
        side="BUY",
        quantity=50,
        price=Decimal("150.00"),
        fee=Decimal("0.25"),
        slippage=Decimal("0.02"),
        commission=Decimal("0.75"),
        partial=True,
    )
    data = event.to_dict()
    restored = FillEvent.from_dict(data)
    assert restored.slippage == Decimal("0.02")
    assert restored.commission == Decimal("0.75")
    assert restored.partial is True


def test_order_event_roundtrips_with_reason():
    event = OrderEvent(
        timestamp=datetime(2026, 6, 22, 10, 15, 0, tzinfo=timezone.utc),
        source=EventSource.BROKER,
        symbol="AAPL.US",
        broker_order_id="bo-124",
        status="REJECTED",
        filled_quantity=0,
        avg_price=None,
        reason="insufficient funds",
    )
    data = event.to_dict()
    restored = OrderEvent.from_dict(data)
    assert restored.reason == "insufficient funds"
