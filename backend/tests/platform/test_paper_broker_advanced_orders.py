from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import BarEvent, EventSource
from app.platform.paper_broker import PaperBroker
from app.platform.sdk import OrderIntent


def _bar(symbol: str, opn: str, high: str, low: str, close: str, minute: int) -> BarEvent:
    return BarEvent(
        timestamp=datetime(2026, 6, 22, 10, minute, tzinfo=timezone.utc),
        source=EventSource.MARKET, symbol=symbol,
        open=Decimal(opn), high=Decimal(high), low=Decimal(low), close=Decimal(close), volume=10000,
    )


def test_stop_buy_triggers_when_high_reaches_stop():
    broker = PaperBroker()
    intent = OrderIntent(symbol="AAPL.US", side="BUY", quantity=10, order_type="STOP", stop_price=Decimal("155"), reason="stop")
    broker.submit(intent)
    # bar high 156 >= 155 -> trigger
    fills = broker.on_bar(_bar("AAPL.US", "150", "156", "149", "154", 0))
    assert len(fills) == 1
    assert fills[0].side == "BUY"


def test_stop_does_not_trigger_before_level():
    broker = PaperBroker()
    intent = OrderIntent(symbol="AAPL.US", side="BUY", quantity=10, order_type="STOP", stop_price=Decimal("155"), reason="stop")
    broker.submit(intent)
    fills = broker.on_bar(_bar("AAPL.US", "150", "152", "149", "151", 0))
    assert fills == []


def test_trailing_sell_stop_ratchets_and_triggers():
    broker = PaperBroker()
    intent = OrderIntent(symbol="AAPL.US", side="SELL", quantity=10, order_type="TRAILING", trailing_offset=Decimal("2"), reason="trail")
    ev = broker.submit(intent)
    # bar1: high 160 -> stop seeded at 158
    broker.on_bar(_bar("AAPL.US", "150", "160", "149", "159", 0))
    assert broker._trailing_stops[ev.broker_order_id] == Decimal("158")
    # bar2: high 162 -> stop ratchets to 160
    broker.on_bar(_bar("AAPL.US", "159", "162", "159", "161", 1))
    assert broker._trailing_stops[ev.broker_order_id] == Decimal("160")
    # bar3: low 159 <= 160 -> trigger SELL
    fills = broker.on_bar(_bar("AAPL.US", "161", "163", "159", "160", 2))
    assert len(fills) == 1
    assert fills[0].side == "SELL"


def test_oco_cancels_partner_on_fill():
    broker = PaperBroker()
    take_profit = OrderIntent(symbol="AAPL.US", side="SELL", quantity=10, order_type="LIMIT", limit_price=Decimal("160"), reason="tp")
    tp_ev = broker.submit(take_profit)
    stop_loss = OrderIntent(symbol="AAPL.US", side="SELL", quantity=10, order_type="STOP", stop_price=Decimal("145"), reason="sl", linked_order_id=tp_ev.broker_order_id)
    sl_ev = broker.submit(stop_loss)
    # also link tp -> sl for full OCO
    broker._orders[tp_ev.broker_order_id].intent = OrderIntent(
        symbol="AAPL.US", side="SELL", quantity=10, order_type="LIMIT", limit_price=Decimal("160"), reason="tp", linked_order_id=sl_ev.broker_order_id,
    )
    # stop loss fills first -> should cancel the take-profit partner
    fills = broker.on_bar(_bar("AAPL.US", "150", "152", "144", "146", 0))
    assert any(f.broker_order_id == sl_ev.broker_order_id for f in fills)
    assert broker._orders[tp_ev.broker_order_id].status == "CANCELLED"
