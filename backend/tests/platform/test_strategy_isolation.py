"""Tests for the P197 multi-strategy concurrent isolation manager."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.platform.events import EventSource, FillEvent
from app.platform.strategy_isolation import StrategyIsolationManager


def _fill(order_id: str, symbol: str, side: str, qty: int, price: str) -> FillEvent:
    return FillEvent(
        timestamp=datetime(2026, 6, 24, tzinfo=timezone.utc),
        source=EventSource.BROKER,
        symbol=symbol,
        broker_order_id=order_id,
        side=side,
        quantity=qty,
        price=Decimal(price),
        commission=Decimal("0"),
    )


def test_allocate_creates_isolated_portfolios():
    mgr = StrategyIsolationManager(total_capital=Decimal("100000"))
    mgr.allocate("alpha", Decimal("60000"))
    mgr.allocate("beta", Decimal("40000"))

    assert mgr.total_allocated() == Decimal("100000")
    assert set(mgr.allocated_strategies()) == {"alpha", "beta"}
    assert mgr.portfolio("alpha").cash == Decimal("60000")
    assert mgr.portfolio("beta").cash == Decimal("40000")
    assert mgr.unallocated_capital() == Decimal("0")


def test_allocate_exceeding_total_raises():
    mgr = StrategyIsolationManager(total_capital=Decimal("100000"))
    mgr.allocate("alpha", Decimal("80000"))
    with pytest.raises(ValueError):
        mgr.allocate("beta", Decimal("30000"))


def test_allocate_duplicate_raises():
    mgr = StrategyIsolationManager(total_capital=Decimal("100000"))
    mgr.allocate("alpha", Decimal("10000"))
    with pytest.raises(ValueError):
        mgr.allocate("alpha", Decimal("10000"))


def test_allocate_weights_sums_to_total():
    mgr = StrategyIsolationManager(total_capital=Decimal("100000"))
    mgr.allocate_weights({"alpha": Decimal("0.6"), "beta": Decimal("0.4")})
    assert mgr.portfolio("alpha").cash == Decimal("60000.00")
    assert mgr.portfolio("beta").cash == Decimal("40000.00")


def test_allocate_weights_exceeding_one_raises():
    mgr = StrategyIsolationManager(total_capital=Decimal("100000"))
    with pytest.raises(ValueError):
        mgr.allocate_weights({"alpha": Decimal("0.7"), "beta": Decimal("0.5")})


def test_fill_routed_to_bound_strategy_only():
    mgr = StrategyIsolationManager(total_capital=Decimal("100000"))
    mgr.allocate("alpha", Decimal("50000"))
    mgr.allocate("beta", Decimal("50000"))
    mgr.bind_order("o1", "alpha")
    mgr.bind_order("o2", "beta")

    consumed = mgr.on_fill(_fill("o1", "A.US", "BUY", 100, "100"))
    mgr.on_fill(_fill("o2", "B.US", "BUY", 50, "200"))

    assert consumed == "alpha"
    # alpha spent 100*100=10000, beta spent 50*200=10000; isolated.
    assert mgr.portfolio("alpha").cash == Decimal("40000")
    assert mgr.portfolio("beta").cash == Decimal("40000")
    assert mgr.portfolio("alpha").position("A.US").quantity == 100
    assert mgr.portfolio("beta").position("B.US").quantity == 50


def test_unbound_fill_is_dropped():
    mgr = StrategyIsolationManager(total_capital=Decimal("100000"))
    mgr.allocate("alpha", Decimal("100000"))
    # No bind_order call.
    result = mgr.on_fill(_fill("o-unknown", "A.US", "BUY", 10, "100"))
    assert result is None
    assert mgr.portfolio("alpha").cash == Decimal("100000")


def test_strategy_pnl_tracks_unrealized_plus_realized():
    mgr = StrategyIsolationManager(total_capital=Decimal("100000"))
    mgr.allocate("alpha", Decimal("50000"))
    mgr.bind_order("o1", "alpha")
    mgr.on_fill(_fill("o1", "A.US", "BUY", 100, "100"))

    prices = {"A.US": Decimal("110")}
    # NAV = 40000 cash + 100*110 = 51000; PnL vs 50000 capital = 1000.
    assert mgr.strategy_nav("alpha", prices) == Decimal("51000")
    assert mgr.strategy_pnl("alpha", prices) == Decimal("1000")


def test_aggregate_nav_sums_strategies_no_double_count():
    mgr = StrategyIsolationManager(total_capital=Decimal("100000"))
    mgr.allocate("alpha", Decimal("50000"))
    mgr.allocate("beta", Decimal("50000"))
    mgr.bind_order("o1", "alpha")
    mgr.bind_order("o2", "beta")
    mgr.on_fill(_fill("o1", "A.US", "BUY", 100, "100"))
    mgr.on_fill(_fill("o2", "B.US", "BUY", 50, "200"))

    prices = {"A.US": Decimal("100"), "B.US": Decimal("200")}
    # No PnL yet (price = cost): NAV should equal initial total capital.
    assert mgr.aggregate_nav(prices) == Decimal("100000")


def test_attribution_snapshot():
    mgr = StrategyIsolationManager(total_capital=Decimal("100000"))
    mgr.allocate("alpha", Decimal("60000"))
    mgr.allocate("beta", Decimal("40000"))
    mgr.bind_order("o1", "alpha")
    mgr.on_fill(_fill("o1", "A.US", "BUY", 100, "100"))

    attr = mgr.attribution(prices={"A.US": Decimal("110")})
    assert set(attr.keys()) == {"alpha", "beta"}
    assert attr["alpha"]["capital"] == Decimal("60000")
    assert attr["alpha"]["total_pnl"] == Decimal("1000")
    assert attr["beta"]["total_pnl"] == Decimal("0")


def test_deallocate_removes_strategy():
    mgr = StrategyIsolationManager(total_capital=Decimal("100000"))
    mgr.allocate("alpha", Decimal("40000"))
    mgr.deallocate("alpha")
    assert "alpha" not in mgr.allocated_strategies()
    assert mgr.total_allocated() == Decimal("0")


def test_partial_allocation_leaves_unallocated_reserve():
    mgr = StrategyIsolationManager(total_capital=Decimal("100000"))
    mgr.allocate("alpha", Decimal("30000"))
    assert mgr.unallocated_capital() == Decimal("70000")
