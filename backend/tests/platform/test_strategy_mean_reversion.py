from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, EventSource
from app.platform.registry import StrategyRegistry
from app.strategies.mean_reversion import MeanReversionStrategy


SYMBOL = "AAPL.US"
PARAMS = {
    "lookback": 5,
    "entry_z": -2.0,
    "exit_z": 0.0,
    "quantity": 3,
}


def _bar(close: str, minute: int) -> BarEvent:
    price = Decimal(close)
    return BarEvent(
        timestamp=datetime(2026, 7, 23, 10, minute, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol=SYMBOL,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=100,
    )


def test_mean_reversion_emits_no_signal_during_warmup() -> None:
    strategy = MeanReversionStrategy(params=PARAMS)
    ctx = StrategyContext(symbol=SYMBOL, positions={}, params=strategy.params)

    intents = [strategy.on_bar(ctx, _bar("100", minute)) for minute in range(4)]

    assert intents == [[], [], [], []]


def test_mean_reversion_buys_when_zscore_is_oversold() -> None:
    strategy = MeanReversionStrategy(params=PARAMS)
    ctx = StrategyContext(symbol=SYMBOL, positions={}, params=strategy.params)
    for minute in range(4):
        strategy.on_bar(ctx, _bar("100", minute))

    intents = strategy.on_bar(ctx, _bar("80", 4))

    assert len(intents) == 1
    assert intents[0].symbol == SYMBOL
    assert intents[0].side == "BUY"
    assert intents[0].quantity == 3
    assert intents[0].order_type == "LIMIT"
    assert intents[0].limit_price == Decimal("80")
    assert intents[0].reason == "zscore_oversold"


def test_mean_reversion_sells_current_position_after_reversion() -> None:
    strategy = MeanReversionStrategy(params=PARAMS)
    flat_ctx = StrategyContext(symbol=SYMBOL, positions={}, params=strategy.params)
    for minute in range(4):
        strategy.on_bar(flat_ctx, _bar("100", minute))
    strategy.on_bar(flat_ctx, _bar("80", 4))
    long_ctx = StrategyContext(
        symbol=SYMBOL,
        positions={SYMBOL: {"quantity": 7}},
        params=strategy.params,
    )

    intents = strategy.on_bar(long_ctx, _bar("100", 5))

    assert len(intents) == 1
    assert intents[0].symbol == SYMBOL
    assert intents[0].side == "SELL"
    assert intents[0].quantity == 7
    assert intents[0].order_type == "LIMIT"
    assert intents[0].limit_price == Decimal("100")
    assert intents[0].reason == "zscore_reverted_to_mean"


def test_mean_reversion_parameter_schema_is_valid() -> None:
    strategy = MeanReversionStrategy(params={})

    assert strategy.parameter_schema == {
        "type": "object",
        "required": ["lookback", "entry_z", "exit_z", "quantity"],
        "properties": {
            "lookback": {"type": "integer"},
            "entry_z": {"type": "number"},
            "exit_z": {"type": "number"},
            "quantity": {"type": "integer"},
        },
    }


def test_mean_reversion_is_auto_discovered() -> None:
    registry = StrategyRegistry()

    registry.discover()

    assert registry.get("mean_reversion") is MeanReversionStrategy
