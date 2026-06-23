from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.bus import EventBus
from app.platform.events import BarEvent, EventSource, FillEvent
from app.platform.projections import DailyReturnsProjection, NavProjection, ProjectionEngine


def _bar(day: int, close: str) -> BarEvent:
    return BarEvent(timestamp=datetime(2026, 6, day, 16, 0, tzinfo=timezone.utc), source=EventSource.MARKET, symbol="A", open=Decimal(close), high=Decimal(close), low=Decimal(close), close=Decimal(close), volume=100)


def _fill(side, qty, price, day) -> FillEvent:
    return FillEvent(timestamp=datetime(2026, 6, day, 10, 0, tzinfo=timezone.utc), source=EventSource.BROKER, symbol="A", broker_order_id="o", side=side, quantity=qty, price=Decimal(price), commission=Decimal("0"))


def test_nav_projection_tracks_series_from_fills_and_bars():
    proj = NavProjection(initial_cash=Decimal("10000"))
    proj.apply(_fill("BUY", 100, "100", 1))   # cash -> 0, 100 @ 100
    proj.apply(_bar(1, "100"))                 # nav = 0 + 100*100 = 10000
    proj.apply(_bar(2, "110"))                 # nav = 100*110 = 11000
    state = proj.state()
    assert len(state["nav_series"]) == 2
    assert state["nav_series"][0]["nav"] == 10000.0
    assert state["nav_series"][1]["nav"] == 11000.0
    assert state["positions"] == {"A": 100}


def test_daily_returns_projection_computes_day_over_day():
    proj = DailyReturnsProjection(initial_cash=Decimal("10000"))
    proj.apply(_fill("BUY", 100, "100", 1))
    proj.apply(_bar(1, "100"))   # eod 10000
    proj.apply(_bar(2, "110"))   # eod 11000 -> +10%
    proj.apply(_bar(3, "99"))    # eod 9900 -> -10%
    state = proj.state()
    rets = state["daily_returns"]
    assert len(rets) == 2
    assert abs(rets[0]["return"] - 0.10) < 1e-9
    assert abs(rets[1]["return"] + 0.10) < 1e-9


def test_projection_engine_dispatches_to_all():
    bus = EventBus()
    engine = ProjectionEngine()
    nav = NavProjection(initial_cash=Decimal("10000"))
    daily = DailyReturnsProjection(initial_cash=Decimal("10000"))
    engine.register(nav)
    engine.register(daily)
    engine.subscribe(bus)
    bus.publish(_fill("BUY", 100, "100", 1))
    bus.publish(_bar(1, "100"))
    bus.publish(_bar(2, "110"))
    state = engine.state()
    assert "nav" in state and "daily_returns" in state
    assert len(state["nav"]["nav_series"]) == 2
    assert len(state["daily_returns"]["daily_returns"]) == 1
