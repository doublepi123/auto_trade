from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import EventSource, FillEvent
from app.platform.portfolio import Portfolio


def _fill(side, qty, price, commission="0"):
    return FillEvent(
        timestamp=datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc),
        source=EventSource.BROKER, symbol="AAPL.US", broker_order_id="o",
        side=side, quantity=qty, price=Decimal(price), commission=Decimal(commission),
    )


def test_buy_updates_cash_and_avg_cost():
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill("BUY", 10, "100"))
    assert p.cash == Decimal("9000")  # 10000 - 10*100
    assert p.positions["AAPL.US"].quantity == 10
    assert p.positions["AAPL.US"].avg_cost == Decimal("100")


def test_sell_realizes_pnl_and_resets_avg_cost():
    p = Portfolio(Decimal("10000"))
    p.on_fill(_fill("BUY", 10, "100"))
    p.on_fill(_fill("SELL", 10, "120", commission="2"))
    assert p.realized_pnl == Decimal("198")  # (120-100)*10 - 2
    assert p.positions["AAPL.US"].quantity == 0
    assert p.positions["AAPL.US"].avg_cost == Decimal("0")
    assert p.cash == Decimal("10000") - Decimal("1000") + Decimal("1200") - Decimal("2")


def test_nav_uses_current_prices():
    p = Portfolio(Decimal("5000"))
    p.on_fill(_fill("BUY", 10, "100"))
    assert p.nav({"AAPL.US": Decimal("130")}) == Decimal("5000") - Decimal("1000") + Decimal("1300")


def test_weighted_avg_cost_on_multiple_buys():
    p = Portfolio(Decimal("100000"))
    p.on_fill(_fill("BUY", 10, "100"))
    p.on_fill(_fill("BUY", 10, "120"))
    pos = p.positions["AAPL.US"]
    assert pos.quantity == 20
    assert pos.avg_cost == Decimal("110")  # (10*100 + 10*120)/20
