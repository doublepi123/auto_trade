from datetime import datetime, timezone
from decimal import Decimal

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, EventSource
from app.strategies.interval_strategy import IntervalStrategy


def make_bar(close: str, high: str = "160", low: str = "140") -> BarEvent:
    return BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("150"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=100,
    )


def test_interval_strategy_buy_below_buy_low():
    strategy = IntervalStrategy(
        params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10}
    )
    ctx = StrategyContext(symbol="AAPL.US", positions={}, params=strategy.params)
    bar = make_bar(close="144")
    intents = strategy.on_bar(ctx, bar)
    assert len(intents) == 1
    assert intents[0].side == "BUY"
    assert intents[0].quantity == 10


def test_interval_strategy_sell_above_sell_high():
    strategy = IntervalStrategy(
        params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10}
    )
    ctx = StrategyContext(symbol="AAPL.US", positions={"AAPL.US": {"quantity": 10}}, params=strategy.params)
    bar = make_bar(close="156")
    intents = strategy.on_bar(ctx, bar)
    assert len(intents) == 1
    assert intents[0].side == "SELL"


def test_interval_strategy_no_signal_in_range():
    strategy = IntervalStrategy(
        params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10}
    )
    ctx = StrategyContext(symbol="AAPL.US", positions={}, params=strategy.params)
    bar = make_bar(close="150")
    intents = strategy.on_bar(ctx, bar)
    assert len(intents) == 0
