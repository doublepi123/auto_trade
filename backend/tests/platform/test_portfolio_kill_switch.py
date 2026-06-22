from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform import portfolio_runner as prm
from app.platform.bus import EventBus
from app.platform.portfolio_config import PortfolioConfig
from app.platform.portfolio_runner import (
    PortfolioRunner,
    is_kill_switch_armed,
    reset_kill_switch_for_tests,
)
from app.platform.risk_engine import RiskEngine
from app.platform.runner import PlatformRunner
from app.strategies.interval_strategy import IntervalStrategy


def _make(config, **kw):
    strategy = IntervalStrategy(
        params={"buy_low": Decimal("1"), "sell_high": Decimal("100000"), "quantity": 1}
    )
    runner = PlatformRunner(symbols=config.symbols, strategy=strategy, mode="paper", bus=EventBus())
    return PortfolioRunner(
        config,
        runner,
        clock=lambda: datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        **kw,
    )


def test_kill_switch_blocks_rebalance():
    reset_kill_switch_for_tests()
    config = PortfolioConfig(name="t", symbols=["AAPL.US"], allocations={"AAPL.US": Decimal("1")})
    pr = _make(config)
    pr.rebalance(prices={"AAPL.US": Decimal("100")}, cash=Decimal("5000"))  # baseline: would BUY 50
    prm.arm_kill_switch()
    assert is_kill_switch_armed() is True
    intents = pr.rebalance(prices={"AAPL.US": Decimal("100")}, cash=Decimal("5000"))
    assert intents == []
    prm.disarm_kill_switch()
    assert is_kill_switch_armed() is False


def test_risk_gate_blocks_on_critical_breach():
    reset_kill_switch_for_tests()
    config = PortfolioConfig(
        name="t",
        symbols=["AAPL.US"],
        allocations={"AAPL.US": Decimal("1")},
        max_gross_exposure=Decimal("0.5"),  # tight cap
    )
    risk_engine = RiskEngine(config=config)
    pr = _make(config, risk_engine=risk_engine)
    # existing position 10 @ 100 = 1000 exposure; cash 100 -> nav 1100;
    # gross ratio = 1000/1100 ~ 0.91 > 0.5 -> CRITICAL
    pr.runner._positions["AAPL.US"] = {"quantity": 10}
    intents = pr.rebalance(prices={"AAPL.US": Decimal("100")}, cash=Decimal("100"))
    assert intents == []


def test_risk_gate_allows_when_within_limits():
    reset_kill_switch_for_tests()
    config = PortfolioConfig(
        name="t",
        symbols=["AAPL.US"],
        allocations={"AAPL.US": Decimal("1")},
        max_gross_exposure=Decimal("5.0"),
    )
    risk_engine = RiskEngine(config=config)
    pr = _make(config, risk_engine=risk_engine)
    intents = pr.rebalance(prices={"AAPL.US": Decimal("100")}, cash=Decimal("5000"))
    assert len(intents) == 1
