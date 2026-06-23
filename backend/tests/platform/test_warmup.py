from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.bus import EventBus
from app.platform.events import BarEvent, Event, EventSource, OrderIntentEvent
from app.platform.indicators import IndicatorService, SMA
from app.platform.runner import PlatformRunner
from app.platform.warmup import WarmupProvider
from app.strategies.interval_strategy import IntervalStrategy


def _bar(close: str, minute: int) -> BarEvent:
    return BarEvent(timestamp=datetime(2026, 6, 23, 10, minute, tzinfo=timezone.utc), source=EventSource.MARKET, symbol="A",
                    open=Decimal("100"), high=Decimal("110"), low=Decimal("90"), close=Decimal(close), volume=100)


def test_warmup_populates_indicators_but_places_no_orders():
    bus = EventBus()
    indicators = IndicatorService([SMA(period=3)])
    strategy = IntervalStrategy(params={"buy_low": Decimal("95"), "sell_high": Decimal("105"), "quantity": 10})
    runner = PlatformRunner(symbols=["A"], strategy=strategy, mode="backtest", bus=bus, indicators=indicators)

    intents: list[OrderIntentEvent] = []

    def _capture(e: Event) -> None:
        if isinstance(e, OrderIntentEvent):
            intents.append(e)

    bus.subscribe("order_intent", _capture)

    history = [_bar("94", 0), _bar("94", 1), _bar("94", 2)]  # would trigger BUY each bar, but warmup suppresses
    WarmupProvider(history).feed(runner)

    assert intents == []  # no order intents during warmup
    assert indicators.value("A", "sma_3") == Decimal("94")  # indicator warmed up


def test_warmup_flag_resets_after_feed():
    bus = EventBus()
    strategy = IntervalStrategy(params={"buy_low": Decimal("95"), "sell_high": Decimal("105"), "quantity": 10})
    runner = PlatformRunner(symbols=["A"], strategy=strategy, mode="backtest", bus=bus)
    assert runner._warming_up is False
    runner.warmup([_bar("94", 0), _bar("94", 1)])
    assert runner._warming_up is False


def test_real_trading_after_warmup_places_orders():
    bus = EventBus()
    strategy = IntervalStrategy(params={"buy_low": Decimal("95"), "sell_high": Decimal("105"), "quantity": 10})
    runner = PlatformRunner(symbols=["A"], strategy=strategy, mode="backtest", bus=bus)
    intents: list[OrderIntentEvent] = []

    def _capture(e: Event) -> None:
        if isinstance(e, OrderIntentEvent):
            intents.append(e)

    bus.subscribe("order_intent", _capture)

    runner.warmup([_bar("94", 0), _bar("94", 1)])  # suppressed
    runner.on_bar(_bar("94", 5))  # real bar -> BUY
    assert len(intents) == 1
