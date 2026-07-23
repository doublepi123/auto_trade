from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, EventSource
from app.platform.registry import StrategyRegistry
from app.strategies.momentum_breakout import MomentumBreakoutStrategy


def _bar(close: str, minute: int) -> BarEvent:
    price = Decimal(close)
    return BarEvent(
        timestamp=datetime(2026, 7, 23, 10, minute, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        event_id=uuid4(),
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=1_000,
    )


def _strategy() -> MomentumBreakoutStrategy:
    return MomentumBreakoutStrategy(
        params={
            "channel_period": 3,
            "atr_period": 2,
            "atr_multiplier": 1.0,
            "quantity": 7,
        }
    )


def test_momentum_breakout_emits_no_signal_during_warmup() -> None:
    strategy = _strategy()
    ctx = StrategyContext(symbol="AAPL.US", positions={}, params=strategy.params)

    intents = [
        strategy.on_bar(ctx, _bar(close, minute))
        for minute, close in enumerate(("100", "101", "200"))
    ]

    assert intents == [[], [], []]


def test_momentum_breakout_buys_above_prior_channel_high() -> None:
    strategy = _strategy()
    ctx = StrategyContext(symbol="AAPL.US", positions={}, params=strategy.params)
    for minute, close in enumerate(("100", "101", "102")):
        strategy.on_bar(ctx, _bar(close, minute))

    intents = strategy.on_bar(ctx, _bar("104", 3))

    assert len(intents) == 1
    assert intents[0].side == "BUY"
    assert intents[0].quantity == 7
    assert intents[0].order_type == "LIMIT"
    assert intents[0].limit_price == Decimal("104")
    assert intents[0].reason == "donchian_breakout"


def test_momentum_breakout_sells_when_atr_trailing_stop_is_hit() -> None:
    strategy = _strategy()
    long_ctx = StrategyContext(
        symbol="AAPL.US",
        positions={"AAPL.US": {"quantity": 7}},
        params=strategy.params,
    )
    strategy.on_bar(long_ctx, _bar("110", 0))
    strategy.on_bar(long_ctx, _bar("108", 1))

    intents = strategy.on_bar(long_ctx, _bar("106", 2))

    assert len(intents) == 1
    assert intents[0].side == "SELL"
    assert intents[0].quantity == 7
    assert intents[0].order_type == "LIMIT"
    assert intents[0].limit_price == Decimal("106")
    assert intents[0].reason == "atr_trailing_stop"


def test_momentum_breakout_parameter_schema_is_valid_and_discoverable() -> None:
    strategy = MomentumBreakoutStrategy(params={})

    schema = strategy.parameter_schema

    assert strategy.name == "momentum_breakout"
    assert strategy.version == "1.0.0"
    assert schema["type"] == "object"
    assert schema["required"] == [
        "channel_period",
        "atr_period",
        "atr_multiplier",
        "quantity",
    ]
    assert schema["properties"] == {
        "channel_period": {"type": "integer", "minimum": 1, "default": 20},
        "atr_period": {"type": "integer", "minimum": 1, "default": 14},
        "atr_multiplier": {
            "type": "number",
            "exclusiveMinimum": 0,
            "default": 2.0,
        },
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
    }
    registry = StrategyRegistry()
    registry.discover()
    assert registry.get("momentum_breakout") is MomentumBreakoutStrategy
