from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import BarEvent, EventSource, QuoteEvent
from app.platform.execution import ExecutionClient, LiveExecutionClient
from app.platform.paper_broker import PaperBroker
from app.platform.sdk import OrderIntent


def test_paper_broker_satisfies_execution_client_protocol():
    broker = PaperBroker()
    assert isinstance(broker, ExecutionClient)


def test_live_execution_client_forwards_to_handler():
    received: list[OrderIntent] = []
    client = LiveExecutionClient(
        lambda intent: received.append(intent),
        clock=lambda: datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc),
    )
    intent = OrderIntent(symbol="AAPL.US", side="BUY", quantity=10, order_type="MARKET", reason="t")
    ev = client.submit(intent)
    assert ev.status == "SUBMITTED"
    assert ev.broker_order_id.startswith("live-")
    assert received == [intent]


def test_live_execution_client_on_bar_returns_no_fills():
    client = LiveExecutionClient(lambda i: None)
    bar = BarEvent(
        timestamp=datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="A",
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=1,
    )
    assert client.on_bar(bar) == []


def test_live_execution_client_on_quote_returns_no_fills():
    client = LiveExecutionClient(lambda i: None)
    quote = QuoteEvent(
        timestamp=datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="A",
        last_price=Decimal("1"),
    )
    assert client.on_quote(quote) == []


def test_live_execution_client_cancel_and_modify_return_events():
    client = LiveExecutionClient(
        lambda i: None,
        clock=lambda: datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc),
    )
    intent = OrderIntent(symbol="A", side="BUY", quantity=1, order_type="MARKET", reason="t")
    assert client.cancel("x").status == "CANCELLED"
    assert client.modify("x", intent).status == "MODIFIED"


def test_live_execution_client_assigns_increasing_ids():
    client = LiveExecutionClient(
        lambda i: None,
        clock=lambda: datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc),
    )
    intent = OrderIntent(symbol="A", side="BUY", quantity=1, order_type="MARKET", reason="t")
    e1 = client.submit(intent)
    e2 = client.submit(intent)
    assert e1.broker_order_id == "live-1"
    assert e2.broker_order_id == "live-2"
