from datetime import datetime, timezone
from decimal import Decimal

from app.database import engine
from app.models import Base
from app.platform.bus import EventBus
from app.platform.events import BarEvent, EventSource
from app.platform.runner import PlatformRunner
from app.platform.store import EventStore
from app.strategies.interval_strategy import IntervalStrategy


def _ensure_tables():
    Base.metadata.create_all(bind=engine)


def make_bar(close: str, ts: datetime) -> BarEvent:
    return BarEvent(
        timestamp=ts,
        source=EventSource.MARKET,
        symbol="AAPL.US",
        open=Decimal("150"),
        high=Decimal("160"),
        low=Decimal("140"),
        close=Decimal(close),
        volume=100,
    )


def test_runner_backtest_generates_fills():
    _ensure_tables()
    bus = EventBus()
    store = EventStore()
    store.clear()

    strategy = IntervalStrategy(params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10})
    runner = PlatformRunner(
        symbol="AAPL.US",
        strategy=strategy,
        mode="backtest",
        bus=bus,
        store=store,
    )

    fills = []
    bus.subscribe("fill", lambda e: fills.append(e))

    t0 = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    runner.on_bar(make_bar("144", t0))
    runner.on_bar(make_bar("156", datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc)))

    assert len(fills) == 2
    assert fills[0].side == "BUY"
    assert fills[1].side == "SELL"


def test_runner_persists_events_to_store():
    _ensure_tables()
    bus = EventBus()
    store = EventStore()
    store.clear()

    strategy = IntervalStrategy(params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10})
    runner = PlatformRunner(symbol="AAPL.US", strategy=strategy, mode="backtest", bus=bus, store=store)

    t0 = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    runner.on_bar(make_bar("144", t0))

    events = store.load(since=datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc))
    assert len(events) >= 3  # bar, order_intent, fill


def test_runner_multi_symbol_routes_bars():
    _ensure_tables()
    bus = EventBus()
    store = EventStore()
    store.clear()

    strategy = IntervalStrategy(params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10})
    runner = PlatformRunner(
        symbols=["AAPL.US", "TSLA.US"],
        strategy=strategy,
        mode="backtest",
        bus=bus,
        store=store,
    )
    assert runner.symbol == "AAPL.US"
    assert runner.symbols == ["AAPL.US", "TSLA.US"]

    fills = []
    bus.subscribe("fill", lambda e: fills.append(e))

    def make_named_bar(symbol: str, close: str, ts: datetime) -> BarEvent:
        return BarEvent(
            timestamp=ts,
            source=EventSource.MARKET,
            symbol=symbol,
            open=Decimal("150"),
            high=Decimal("160"),
            low=Decimal("140"),
            close=Decimal(close),
            volume=100,
        )

    t0 = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    # AAPL buy trigger (close 144 < buy_low 145)
    runner.on_bar(make_named_bar("AAPL.US", "144", t0))
    assert any(f.symbol == "AAPL.US" for f in fills)
