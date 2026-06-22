from decimal import Decimal

from app.platform.portfolio_allocator import PortfolioAllocator
from app.platform.portfolio_config import PortfolioConfig


def test_allocator_generates_rebalance_orders():
    config = PortfolioConfig(
        name="test",
        symbols=["AAPL.US", "TSLA.US"],
        allocations={"AAPL.US": Decimal("0.6"), "TSLA.US": Decimal("0.4")},
    )
    allocator = PortfolioAllocator(config)
    # AAPL already at target; only TSLA needs to be bought
    positions = {"AAPL.US": {"quantity": 43}, "TSLA.US": {"quantity": 0}}
    cash = Decimal("4300")
    prices = {"AAPL.US": Decimal("150"), "TSLA.US": Decimal("200")}

    intents = allocator.rebalance(positions, prices, cash)
    assert len(intents) == 1
    assert intents[0].symbol == "TSLA.US"
    assert intents[0].side == "BUY"
    assert intents[0].quantity == 22
    assert intents[0].order_type == "LIMIT"
    assert intents[0].limit_price == Decimal("200")
    assert intents[0].reason == "portfolio_rebalance"


def test_allocator_generates_sell_when_overweight():
    config = PortfolioConfig(
        name="test",
        symbols=["AAPL.US", "TSLA.US"],
        allocations={"AAPL.US": Decimal("0.5"), "TSLA.US": Decimal("0.5")},
    )
    allocator = PortfolioAllocator(config)
    # Portfolio: 20 AAPL @ $100 = $2000, $0 cash, $0 TSLA
    # Total = $2000. AAPL target = $1000 (50%), so 10 shares overweight.
    positions = {"AAPL.US": {"quantity": 20}, "TSLA.US": {"quantity": 0}}
    cash = Decimal("0")
    prices = {"AAPL.US": Decimal("100"), "TSLA.US": Decimal("200")}
    intents = allocator.rebalance(positions, prices, cash)
    aapl_intent = next((i for i in intents if i.symbol == "AAPL.US"), None)
    assert aapl_intent is not None
    assert aapl_intent.side == "SELL"
    assert aapl_intent.quantity == 10


def test_allocator_skips_symbol_with_zero_price():
    config = PortfolioConfig(
        name="test",
        symbols=["AAPL.US", "TSLA.US"],
        allocations={"AAPL.US": Decimal("0.5"), "TSLA.US": Decimal("0.5")},
    )
    allocator = PortfolioAllocator(config)
    positions = {"AAPL.US": {"quantity": 0}, "TSLA.US": {"quantity": 0}}
    cash = Decimal("10000")
    prices = {"AAPL.US": Decimal("100"), "TSLA.US": Decimal("0")}
    intents = allocator.rebalance(positions, prices, cash)
    assert all(i.symbol != "TSLA.US" for i in intents)
