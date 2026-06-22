from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import BarEvent, EventSource
from app.platform.paper_broker import PaperBroker, PaperBrokerConfig
from app.platform.sdk import OrderIntent


def test_paper_broker_fills_limit_buy():
    broker = PaperBroker()
    intent = OrderIntent(
        symbol="AAPL.US",
        side="BUY",
        quantity=100,
        order_type="LIMIT",
        limit_price=Decimal("145"),
        reason="test",
    )
    order_event = broker.submit(intent)
    assert order_event.status == "SUBMITTED"

    bar = BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("146"),
        high=Decimal("146"),
        low=Decimal("144"),
        close=Decimal("144.5"),
        volume=10000,
    )
    fills = broker.on_bar(bar)
    assert len(fills) >= 1
    assert fills[0].side == "BUY"
    assert fills[0].quantity <= 100


def test_paper_broker_fills_limit_sell():
    broker = PaperBroker()
    intent = OrderIntent(
        symbol="AAPL.US",
        side="SELL",
        quantity=10,
        order_type="LIMIT",
        limit_price=Decimal("150"),
        reason="test",
    )
    broker.submit(intent)
    bar = BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("149"),
        high=Decimal("151"),
        low=Decimal("149"),
        close=Decimal("150"),
        volume=10000,
    )
    fills = broker.on_bar(bar)
    assert len(fills) == 1
    assert fills[0].side == "SELL"
    assert fills[0].quantity == 10


def test_paper_broker_partial_fill_respects_probability():
    config = PaperBrokerConfig(partial_fill_probability=0.5)
    broker = PaperBroker(config=config)
    intent = OrderIntent(
        symbol="AAPL.US",
        side="BUY",
        quantity=100,
        order_type="LIMIT",
        limit_price=Decimal("145"),
        reason="test",
    )
    broker.submit(intent)
    bar = BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("146"),
        high=Decimal("146"),
        low=Decimal("144"),
        close=Decimal("144.5"),
        volume=10000,
    )
    fills = broker.on_bar(bar)
    assert len(fills) == 1
    assert fills[0].quantity == 50
    assert fills[0].partial is True


def test_paper_broker_records_slippage_and_commission():
    config = PaperBrokerConfig(slippage_ticks=Decimal("0.05"), commission_rate=Decimal("0.001"))
    broker = PaperBroker(config=config)
    intent = OrderIntent(
        symbol="AAPL.US",
        side="BUY",
        quantity=10,
        order_type="LIMIT",
        limit_price=Decimal("145"),
        reason="test",
    )
    broker.submit(intent)
    bar = BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("144"),
        high=Decimal("146"),
        low=Decimal("144"),
        close=Decimal("145"),
        volume=10000,
    )
    fills = broker.on_bar(bar)
    assert len(fills) == 1
    assert fills[0].slippage == Decimal("0.05")
    assert fills[0].commission == Decimal("1.4405")  # 144.05 * 10 * 0.001


def test_paper_broker_cancel_and_modify():
    broker = PaperBroker()
    intent = OrderIntent(
        symbol="AAPL.US",
        side="BUY",
        quantity=10,
        order_type="LIMIT",
        limit_price=Decimal("145"),
        reason="test",
    )
    submitted = broker.submit(intent)
    cancelled = broker.cancel(submitted.broker_order_id)
    assert cancelled.status == "CANCELLED"
    rejected_cancel = broker.cancel(submitted.broker_order_id)
    assert rejected_cancel.status == "REJECTED"

    new_intent = OrderIntent(
        symbol="AAPL.US",
        side="BUY",
        quantity=20,
        order_type="LIMIT",
        limit_price=Decimal("146"),
        reason="test",
    )
    modify_event = broker.modify(submitted.broker_order_id, new_intent)
    assert modify_event.status == "REJECTED"
