from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.bus import EventBus
from app.platform.events import EventSource, FillEvent, OrderEvent, OrderIntentEvent
from app.platform.oms import OrderManagementSystem


def _ts(minute: int) -> datetime:
    return datetime(2026, 6, 23, 10, minute, tzinfo=timezone.utc)


def test_oms_tracks_submit_and_fill_and_marks_filled():
    bus = EventBus()
    oms = OrderManagementSystem()
    oms.subscribe(bus)
    # intent then order submitted
    bus.publish(OrderIntentEvent(timestamp=_ts(0), source=EventSource.STRATEGY, symbol="A", side="BUY", quantity=10, order_type="LIMIT", limit_price=Decimal("100"), reason="t"))
    bus.publish(OrderEvent(timestamp=_ts(0), source=EventSource.BROKER, symbol="A", broker_order_id="o1", status="SUBMITTED"))
    rec = oms.get("o1")
    assert rec is not None
    assert rec.side == "BUY"
    assert rec.quantity == 10
    assert rec.order_type == "LIMIT"
    # fill
    bus.publish(FillEvent(timestamp=_ts(1), source=EventSource.BROKER, symbol="A", broker_order_id="o1", side="BUY", quantity=10, price=Decimal("101")))
    rec = oms.get("o1")
    assert rec is not None
    assert rec.status == "FILLED"
    assert rec.filled_quantity == 10
    assert rec.avg_fill_price == Decimal("101")
    assert len(rec.fills) == 1


def test_oms_partial_fill_status():
    bus = EventBus()
    oms = OrderManagementSystem()
    oms.subscribe(bus)
    bus.publish(OrderIntentEvent(timestamp=_ts(0), source=EventSource.STRATEGY, symbol="A", side="BUY", quantity=10, order_type="LIMIT", limit_price=Decimal("100"), reason="t"))
    bus.publish(OrderEvent(timestamp=_ts(0), source=EventSource.BROKER, symbol="A", broker_order_id="o2", status="SUBMITTED"))
    bus.publish(FillEvent(timestamp=_ts(1), source=EventSource.BROKER, symbol="A", broker_order_id="o2", side="BUY", quantity=4, price=Decimal("100")))
    rec = oms.get("o2")
    assert rec is not None
    assert rec.status == "PARTIAL_FILLED"
    assert rec.filled_quantity == 4


def test_oms_weighted_avg_fill_price():
    bus = EventBus()
    oms = OrderManagementSystem()
    oms.subscribe(bus)
    bus.publish(OrderEvent(timestamp=_ts(0), source=EventSource.BROKER, symbol="A", broker_order_id="o3", status="SUBMITTED"))
    bus.publish(FillEvent(timestamp=_ts(1), source=EventSource.BROKER, symbol="A", broker_order_id="o3", side="BUY", quantity=10, price=Decimal("100")))
    bus.publish(FillEvent(timestamp=_ts(2), source=EventSource.BROKER, symbol="A", broker_order_id="o3", side="BUY", quantity=10, price=Decimal("120")))
    rec = oms.get("o3")
    assert rec is not None
    assert rec.avg_fill_price == Decimal("110")
    assert rec.filled_quantity == 20


def test_oms_queries_by_status_and_open():
    bus = EventBus()
    oms = OrderManagementSystem()
    oms.subscribe(bus)
    bus.publish(OrderEvent(timestamp=_ts(0), source=EventSource.BROKER, symbol="A", broker_order_id="open1", status="SUBMITTED"))
    bus.publish(OrderEvent(timestamp=_ts(0), source=EventSource.BROKER, symbol="A", broker_order_id="cancelled1", status="CANCELLED"))
    assert {r.broker_order_id for r in oms.by_status("CANCELLED")} == {"cancelled1"}
    assert {r.broker_order_id for r in oms.open_orders()} == {"open1"}
    assert len(oms.all()) == 2
