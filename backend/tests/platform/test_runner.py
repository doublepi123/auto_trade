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


from app.platform.risk_engine import RiskEngine
from app.platform.portfolio_config import PortfolioConfig


def test_runner_feeds_risk_engine_with_fills():
    _ensure_tables()
    bus = EventBus()
    store = EventStore()
    store.clear()

    config = PortfolioConfig(
        name="test",
        symbols=["AAPL.US"],
        allocations={"AAPL.US": Decimal("1")},
    )
    risk_engine = RiskEngine(config=config)
    strategy = IntervalStrategy(params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10})
    runner = PlatformRunner(
        symbols=["AAPL.US"],
        strategy=strategy,
        mode="backtest",
        bus=bus,
        store=store,
        risk_engine=risk_engine,
    )

    t0 = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    runner.on_bar(make_bar("144", t0))

    assert risk_engine._positions.get("AAPL.US", {}).get("quantity") == 10


def test_runner_universe_gates_strategy_routing():
    from app.platform.universe import StaticUniverse

    _ensure_tables()
    bus = EventBus()
    strategy = IntervalStrategy(params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10})
    runner = PlatformRunner(
        symbols=["AAPL.US", "TSLA.US"],
        strategy=strategy,
        mode="backtest",
        bus=bus,
        universe=StaticUniverse(["AAPL.US"]),
    )
    fills = []
    bus.subscribe("fill", lambda e: fills.append(e))
    t0 = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)
    # TSLA bar below buy_low but universe excludes it -> no strategy intent -> no fill
    runner.on_bar(
        BarEvent(
            timestamp=t0,
            source=EventSource.MARKET,
            symbol="TSLA.US",
            open=Decimal("150"),
            high=Decimal("160"),
            low=Decimal("140"),
            close=Decimal("144"),
            volume=1000,
        )
    )
    assert fills == []
    # AAPL bar below buy_low and in universe -> BUY fill
    runner.on_bar(
        BarEvent(
            timestamp=datetime(2026, 6, 23, 10, 1, tzinfo=timezone.utc),
            source=EventSource.MARKET,
            symbol="AAPL.US",
            open=Decimal("150"),
            high=Decimal("160"),
            low=Decimal("140"),
            close=Decimal("144"),
            volume=1000,
        )
    )
    assert any(f.symbol == "AAPL.US" for f in fills)


def test_runner_live_mode_uses_live_execution_client():
    """Live runner with a handler should construct a LiveExecutionClient broker
    so submit emits an OrderEvent in addition to forwarding the intent."""
    _ensure_tables()
    bus = EventBus()
    strategy = IntervalStrategy(
        params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10}
    )
    handled: list = []
    runner = PlatformRunner(
        symbols=["AAPL.US"],
        strategy=strategy,
        mode="live",
        bus=bus,
        live_order_handler=lambda intent: handled.append(intent),
    )
    assert runner._broker is not None  # LiveExecutionClient auto-attached
    orders_emitted: list = []
    bus.subscribe("order", lambda e: orders_emitted.append(e))
    runner.on_bar(
        BarEvent(
            timestamp=datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc),
            source=EventSource.MARKET,
            symbol="AAPL.US",
            open=Decimal("150"),
            high=Decimal("160"),
            low=Decimal("140"),
            close=Decimal("144"),
            volume=1000,
        )
    )
    assert len(handled) == 1
    assert len(orders_emitted) == 1
    assert orders_emitted[0].broker_order_id.startswith("live-")


def test_runner_live_mode_without_handler_stays_brokerless():
    """main.py lifespan builds a live runner with no handler — must remain broker-less
    so the historical `_execute_intent` fallback path is preserved."""
    _ensure_tables()
    bus = EventBus()
    strategy = IntervalStrategy(
        params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10}
    )
    runner = PlatformRunner(
        symbols=["AAPL.US"],
        strategy=strategy,
        mode="live",
        bus=bus,
    )
    assert runner._broker is None
    assert runner.live_order_handler is None


def test_runner_invokes_scheduler_on_bar():
    from app.platform.scheduler import Scheduler

    _ensure_tables()
    bus = EventBus()
    strategy = IntervalStrategy(
        params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10}
    )
    sched = Scheduler()
    fired: list = []
    sched.every_bars(1, lambda b: fired.append(b))
    runner = PlatformRunner(
        symbols=["AAPL.US"],
        strategy=strategy,
        mode="backtest",
        bus=bus,
        scheduler=sched,
    )
    runner.on_bar(
        BarEvent(
            timestamp=datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc),
            source=EventSource.MARKET,
            symbol="AAPL.US",
            open=Decimal("150"),
            high=Decimal("160"),
            low=Decimal("140"),
            close=Decimal("144"),
            volume=1000,
        )
    )
    assert len(fired) == 1
