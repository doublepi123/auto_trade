from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, EventSource
from app.platform.sdk import OrderIntent
from app.platform.strategy_combinator import StrategyCombinator


class _FixedStrategy:
    """Test double: always returns a fixed list of OrderIntents, ignoring input."""

    def __init__(self, name: str, intents: list[OrderIntent]) -> None:
        self._name = name
        self._intents = intents
        self.params: dict = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return "1.0"

    @property
    def parameter_schema(self) -> dict:
        return {}

    def on_bar(self, ctx, bar):
        return list(self._intents)

    def on_quote(self, ctx, quote):
        return list(self._intents)

    def on_fill(self, ctx, fill):
        return []


def _ctx() -> StrategyContext:
    return StrategyContext(symbol="A", positions={}, params={}, clock=lambda: datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc))


def _bar() -> BarEvent:
    return BarEvent(timestamp=datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc), source=EventSource.MARKET, symbol="A", open=Decimal("1"), high=Decimal("1"), low=Decimal("1"), close=Decimal("1"), volume=1)


def test_combinator_aggregates_same_symbol_side_additively():
    s1 = _FixedStrategy("s1", [OrderIntent(symbol="A", side="BUY", quantity=10, order_type="LIMIT", limit_price=Decimal("100"), reason="x")])
    s2 = _FixedStrategy("s2", [OrderIntent(symbol="A", side="BUY", quantity=20, order_type="LIMIT", limit_price=Decimal("100"), reason="x")])
    combo = StrategyCombinator(strategies=[(s1, 1.0), (s2, 1.0)])
    out = combo.on_bar(_ctx(), _bar())
    assert len(out) == 1
    assert out[0].quantity == 30
    assert out[0].symbol == "A" and out[0].side == "BUY"


def test_combinator_applies_weights():
    s1 = _FixedStrategy("s1", [OrderIntent(symbol="A", side="BUY", quantity=100, order_type="LIMIT", limit_price=Decimal("100"), reason="x")])
    combo = StrategyCombinator(strategies=[(s1, 0.25)])  # 100 * 0.25 = 25
    out = combo.on_bar(_ctx(), _bar())
    assert out[0].quantity == 25


def test_combinator_separates_opposing_sides():
    s1 = _FixedStrategy("s1", [OrderIntent(symbol="A", side="BUY", quantity=10, order_type="LIMIT", limit_price=Decimal("100"), reason="x")])
    s2 = _FixedStrategy("s2", [OrderIntent(symbol="A", side="SELL", quantity=5, order_type="LIMIT", limit_price=Decimal("100"), reason="x")])
    combo = StrategyCombinator(strategies=[(s1, 1.0), (s2, 1.0)])
    out = combo.on_bar(_ctx(), _bar())
    sides = {o.side for o in out}
    assert sides == {"BUY", "SELL"}


def test_combinator_separates_symbols():
    s1 = _FixedStrategy("s1", [
        OrderIntent(symbol="A", side="BUY", quantity=10, order_type="LIMIT", limit_price=Decimal("100"), reason="x"),
        OrderIntent(symbol="B", side="BUY", quantity=7, order_type="LIMIT", limit_price=Decimal("200"), reason="x"),
    ])
    combo = StrategyCombinator(strategies=[(s1, 1.0)])
    out = combo.on_bar(_ctx(), _bar())
    assert {o.symbol for o in out} == {"A", "B"}


def test_combinator_empty_when_all_zero():
    s1 = _FixedStrategy("s1", [OrderIntent(symbol="A", side="BUY", quantity=10, order_type="LIMIT", limit_price=Decimal("100"), reason="x")])
    combo = StrategyCombinator(strategies=[(s1, 0.0)])  # weight 0 -> 0 qty
    out = combo.on_bar(_ctx(), _bar())
    assert out == []
