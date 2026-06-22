from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.bus import EventBus
from app.platform.events import BarEvent, Event, EventSource
from app.platform.portfolio_config import PortfolioConfig
from app.platform.portfolio_runner import PortfolioRunner
from app.platform.runner import PlatformRunner
from app.strategies.interval_strategy import IntervalStrategy


def _make_runner(symbols):
    strategy = IntervalStrategy(params={"buy_low": Decimal("1"), "sell_high": Decimal("100000"), "quantity": 1})
    return PlatformRunner(symbols=symbols, strategy=strategy, mode="paper", bus=EventBus())


def test_portfolio_runner_generates_and_submits_rebalance_intents():
    config = PortfolioConfig(
        name="t",
        symbols=["AAPL.US", "TSLA.US"],
        allocations={"AAPL.US": Decimal("0.5"), "TSLA.US": Decimal("0.5")},
    )
    runner = _make_runner(config.symbols)
    emitted: list[Event] = []
    runner.bus.subscribe("order_intent", lambda e: emitted.append(e))
    pr = PortfolioRunner(config, runner, clock=lambda: datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc))
    intents = pr.rebalance(prices={"AAPL.US": Decimal("150"), "TSLA.US": Decimal("200")}, cash=Decimal("10000"))
    assert len(intents) >= 1
    # intents were submitted -> OrderIntentEvents emitted
    assert len(emitted) == len(intents)
    assert all(i.symbol in config.symbols for i in intents)


def test_portfolio_runner_uses_providers_when_args_omitted():
    config = PortfolioConfig(name="t", symbols=["AAPL.US"], allocations={"AAPL.US": Decimal("1")})
    runner = _make_runner(config.symbols)
    pr = PortfolioRunner(
        config, runner,
        prices_provider=lambda: {"AAPL.US": Decimal("100")},
        cash_provider=lambda: Decimal("5000"),
        clock=lambda: datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
    )
    intents = pr.rebalance()
    assert len(intents) == 1
    assert intents[0].side == "BUY"
    assert intents[0].quantity == 50  # 5000 / 100


def test_portfolio_runner_rebalance_orders_fill_on_bars():
    config = PortfolioConfig(name="t", symbols=["AAPL.US"], allocations={"AAPL.US": Decimal("1")})
    runner = _make_runner(config.symbols)
    pr = PortfolioRunner(config, runner, clock=lambda: datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc))
    # cash 5000, price 100 -> BUY 50 @ limit 100
    pr.rebalance(prices={"AAPL.US": Decimal("100")}, cash=Decimal("5000"))
    # feed a bar where low <= 100 so the LIMIT BUY fills
    runner.on_bar(
        BarEvent(
            timestamp=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
            source=EventSource.MARKET, symbol="AAPL.US",
            open=Decimal("100"), high=Decimal("101"), low=Decimal("99"), close=Decimal("100"), volume=1000,
        )
    )
    assert runner._positions.get("AAPL.US", {}).get("quantity") == 50
