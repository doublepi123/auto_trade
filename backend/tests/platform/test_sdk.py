from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, EventSource, FillEvent, QuoteEvent
from app.platform.sdk import OrderIntent, Strategy


class DummyStrategy:
    name = "dummy"
    version = "1.0.0"
    parameter_schema = {"type": "object", "properties": {}}

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> list[OrderIntent]:
        assert bar.symbol is not None
        return [OrderIntent(symbol=bar.symbol, side="BUY", quantity=10, order_type="LIMIT", limit_price=bar.close, reason="test")]

    def on_quote(self, ctx: StrategyContext, quote: QuoteEvent) -> list[OrderIntent]:
        return []

    def on_fill(self, ctx: StrategyContext, fill: FillEvent) -> list[OrderIntent]:
        return []


def test_strategy_protocol_accepted():
    strategy: Strategy = DummyStrategy()
    assert strategy.name == "dummy"
    assert strategy.version == "1.0.0"


def test_strategy_on_bar_emits_order_intent():
    strategy: Strategy = DummyStrategy()
    ctx = StrategyContext(symbol="AAPL.US", positions={}, params={}, clock=lambda: datetime.now(timezone.utc))
    bar = BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("150"),
        high=Decimal("151"),
        low=Decimal("149"),
        close=Decimal("150.5"),
        volume=100,
    )
    intents = strategy.on_bar(ctx, bar)
    assert len(intents) == 1
    assert intents[0].side == "BUY"
    assert intents[0].limit_price == Decimal("150.5")


def test_strategy_on_quote_returns_empty():
    strategy: Strategy = DummyStrategy()
    ctx = StrategyContext(symbol="AAPL.US", positions={}, params={}, clock=lambda: datetime.now(timezone.utc))
    quote = QuoteEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        last_price=Decimal("150.5"),
        bid=Decimal("150"),
        ask=Decimal("151"),
        volume=1000,
    )
    intents = strategy.on_quote(ctx, quote)
    assert intents == []


def test_strategy_on_fill_returns_empty():
    strategy: Strategy = DummyStrategy()
    ctx = StrategyContext(symbol="AAPL.US", positions={}, params={}, clock=lambda: datetime.now(timezone.utc))
    fill = FillEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        source=EventSource.BROKER,
        symbol="AAPL.US",
        broker_order_id="ord-123",
        side="BUY",
        quantity=10,
        price=Decimal("150.5"),
    )
    intents = strategy.on_fill(ctx, fill)
    assert intents == []
