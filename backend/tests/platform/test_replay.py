from datetime import datetime, timezone
from decimal import Decimal

from app.database import engine
from app.models import Base
from app.platform.bus import EventBus
from app.platform.events import BarEvent, Event, EventSource
from app.platform.runner import PlatformRunner
from app.platform.replay import EventReplayer
from app.platform.store import EventStore
from app.strategies.interval_strategy import IntervalStrategy


def test_replay_produces_same_fills():
    Base.metadata.create_all(bind=engine)
    bus = EventBus()
    store = EventStore()
    store.clear()

    strategy = IntervalStrategy(params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10})
    runner = PlatformRunner(symbol="AAPL.US", strategy=strategy, mode="backtest", bus=bus, store=store)

    fills_during_run = []
    bus.subscribe("fill", lambda e: fills_during_run.append(e))

    t0 = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    runner.on_bar(BarEvent(
        timestamp=t0,
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("150"),
        high=Decimal("160"),
        low=Decimal("140"),
        close=Decimal("144"),
        volume=100,
    ))
    runner.on_bar(BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("150"),
        high=Decimal("160"),
        low=Decimal("140"),
        close=Decimal("156"),
        volume=100,
    ))

    replayer = EventReplayer(store)
    fills_during_replay = []
    replay_bus = EventBus()
    replay_bus.subscribe("fill", lambda e: fills_during_replay.append(e))
    replayer.replay(since=datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc), bus=replay_bus)

    assert len(fills_during_replay) == len(fills_during_run)
    assert fills_during_replay[0].side == fills_during_run[0].side
    assert fills_during_replay[1].side == fills_during_run[1].side


def test_replay_without_bus_returns_events():
    Base.metadata.create_all(bind=engine)
    bus = EventBus()
    store = EventStore()
    store.clear()

    strategy = IntervalStrategy(params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10})
    runner = PlatformRunner(symbol="AAPL.US", strategy=strategy, mode="backtest", bus=bus, store=store)

    t0 = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    runner.on_bar(BarEvent(
        timestamp=t0,
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("150"),
        high=Decimal("160"),
        low=Decimal("140"),
        close=Decimal("144"),
        volume=100,
    ))

    replayer = EventReplayer(store)
    events = replayer.replay(since=datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc))
    assert len(events) >= 3  # bar, order_intent, fill
    assert all(isinstance(e, Event) for e in events)


def test_replay_empty_store_returns_empty():
    Base.metadata.create_all(bind=engine)
    store = EventStore()
    store.clear()
    replayer = EventReplayer(store)
    events = replayer.replay()
    assert events == []
