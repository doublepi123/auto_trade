from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

from app.database import engine
from app.models import Base
from app.platform.bus import EventBus
from app.platform.events import BarEvent, EventSource
from app.platform.registry import StrategyRegistry
from app.platform.replay import EventReplayer
from app.platform.runner import PlatformRunner
from app.platform.sdk import Strategy
from app.platform.store import EventStore
from app.strategies.interval_strategy import IntervalStrategy


def test_full_backtest_round_trip():
    Base.metadata.create_all(bind=engine)
    bus = EventBus()
    store = EventStore()
    store.clear()

    registry = StrategyRegistry()
    registry.register(IntervalStrategy)
    strategy_cls = registry.get("interval")
    strategy = cast(Any, strategy_cls)(params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10})

    runner = PlatformRunner(symbol="AAPL.US", strategy=strategy, mode="backtest", bus=bus, store=store)

    fills = []
    bus.subscribe("fill", lambda e: fills.append(e))

    base = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    prices = ["144", "146", "156"]
    for i, close in enumerate(prices):
        runner.on_bar(BarEvent(
            timestamp=base.replace(minute=i),
            source=EventSource.MARKET,
            symbol="AAPL.US",
            open=Decimal("150"),
            high=Decimal("160"),
            low=Decimal("140"),
            close=Decimal(close),
            volume=100,
        ))

    assert len(fills) == 2
    assert fills[0].side == "BUY"
    assert fills[1].side == "SELL"

    replay_bus = EventBus()
    replay_fills = []
    replay_bus.subscribe("fill", lambda e: replay_fills.append(e))
    replayer = EventReplayer(store)
    replayer.replay(bus=replay_bus)

    assert len(replay_fills) == len(fills)
    for original, replayed in zip(fills, replay_fills, strict=True):
        assert replayed.side == original.side
        assert replayed.quantity == original.quantity
        assert replayed.price == original.price
