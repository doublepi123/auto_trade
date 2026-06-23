from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import EventSource, FillEvent
from app.platform.position_engine import PositionEngine


def _fill(side, qty, price, commission="0"):
    return FillEvent(
        timestamp=datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc),
        source=EventSource.BROKER,
        symbol="A",
        broker_order_id="o",
        side=side,
        quantity=qty,
        price=Decimal(price),
        commission=Decimal(commission),
    )


def test_open_long_on_buy():
    eng = PositionEngine()
    eng.on_fill(_fill("BUY", 10, "100"))
    pos = eng.position("A")
    assert pos.side == "LONG"
    assert pos.quantity == 10
    assert pos.avg_cost == Decimal("100")
    assert pos.realized_pnl == Decimal("0")


def test_increase_long_weighted_avg():
    eng = PositionEngine()
    eng.on_fill(_fill("BUY", 10, "100"))
    eng.on_fill(_fill("BUY", 10, "120"))
    pos = eng.position("A")
    assert pos.side == "LONG"
    assert pos.quantity == 20
    assert pos.avg_cost == Decimal("110")


def test_close_long_realizes_pnl():
    eng = PositionEngine()
    eng.on_fill(_fill("BUY", 10, "100"))
    eng.on_fill(_fill("SELL", 10, "130", commission="2"))
    pos = eng.position("A")
    assert pos.side == "FLAT"
    assert pos.quantity == 0
    assert pos.realized_pnl == Decimal("298")  # (130-100)*10 - 2


def test_flip_long_to_short_realizes_leg_pnl():
    eng = PositionEngine()
    eng.on_fill(_fill("BUY", 10, "100"))
    # SELL 15: close 10 long @130 (realize 300), open SHORT 5 @130
    eng.on_fill(_fill("SELL", 15, "130"))
    pos = eng.position("A")
    assert pos.side == "SHORT"
    assert pos.quantity == 5
    assert pos.avg_cost == Decimal("130")
    assert pos.realized_pnl == Decimal("300")


def test_open_short_on_sell():
    eng = PositionEngine()
    eng.on_fill(_fill("SELL", 5, "100"))
    pos = eng.position("A")
    assert pos.side == "SHORT"
    assert pos.quantity == 5


def test_close_short_realizes_pnl():
    eng = PositionEngine()
    eng.on_fill(_fill("SELL", 10, "100"))
    # buy back at 90 -> profit (100-90)*10 = 100
    eng.on_fill(_fill("BUY", 10, "90"))
    pos = eng.position("A")
    assert pos.side == "FLAT"
    assert pos.realized_pnl == Decimal("100")


def test_open_positions_filter():
    eng = PositionEngine()
    eng.on_fill(_fill("BUY", 10, "100"))
    assert len(eng.open_positions()) == 1
    eng.on_fill(_fill("SELL", 10, "100"))
    assert eng.open_positions() == []
