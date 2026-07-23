from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, EventSource
from app.platform.registry import get_default_registry
from app.strategies.trend_following import TrendFollowingStrategy


_SYMBOL = "AAPL.US"


def _bar(close: str, minute: int) -> BarEvent:
    price = Decimal(close)
    return BarEvent(
        timestamp=datetime(2026, 7, 23, 10, minute, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol=_SYMBOL,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=1000,
    )


def _strategy() -> TrendFollowingStrategy:
    return TrendFollowingStrategy(
        params={
            "fast_period": 2,
            "slow_period": 3,
            "atr_period": 2,
            "atr_threshold_pct": 0.10,
            "quantity": 5,
        }
    )


def test_no_signal_during_warmup() -> None:
    # Given
    strategy = _strategy()
    ctx = StrategyContext(symbol=_SYMBOL, positions={}, params=strategy.params)

    # When
    intents = [strategy.on_bar(ctx, _bar(close, minute)) for minute, close in enumerate(("10", "9", "9"))]

    # Then
    assert intents == [[], [], []]


def test_bullish_crossover_buys_when_atr_confirms_trend() -> None:
    # Given
    strategy = _strategy()
    ctx = StrategyContext(symbol=_SYMBOL, positions={}, params=strategy.params)
    for minute, close in enumerate(("10", "9", "9")):
        strategy.on_bar(ctx, _bar(close, minute))

    # When
    intents = strategy.on_bar(ctx, _bar("12", 3))

    # Then
    assert len(intents) == 1
    assert intents[0].side == "BUY"
    assert intents[0].quantity == 5
    assert intents[0].limit_price == Decimal("12")
    assert intents[0].reason == "ma_golden_cross"


def test_bearish_crossover_sells_long_position() -> None:
    # Given
    strategy = _strategy()
    ctx = StrategyContext(
        symbol=_SYMBOL,
        positions={_SYMBOL: {"quantity": 7}},
        params=strategy.params,
    )
    for minute, close in enumerate(("10", "11", "11")):
        strategy.on_bar(ctx, _bar(close, minute))

    # When
    intents = strategy.on_bar(ctx, _bar("8", 3))

    # Then
    assert len(intents) == 1
    assert intents[0].side == "SELL"
    assert intents[0].quantity == 7
    assert intents[0].limit_price == Decimal("8")
    assert intents[0].reason == "ma_death_cross"


def test_parameter_schema_is_valid_and_strategy_is_auto_discovered() -> None:
    # Given
    strategy = TrendFollowingStrategy(params={})

    # When
    schema = strategy.parameter_schema
    discovered_class = get_default_registry().get("trend_following")

    # Then
    assert schema["type"] == "object"
    assert schema["required"] == ["fast_period", "slow_period", "atr_period", "quantity"]
    assert schema["properties"] == {
        "fast_period": {"type": "integer"},
        "slow_period": {"type": "integer"},
        "atr_period": {"type": "integer"},
        "atr_threshold_pct": {"type": "number"},
        "quantity": {"type": "integer"},
    }
    assert discovered_class is TrendFollowingStrategy
