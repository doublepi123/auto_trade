from datetime import datetime, timezone
from decimal import Decimal

from app.platform.portfolio_config import PortfolioConfig
from app.platform.portfolio_risk import PortfolioRiskController


def test_risk_controller_detects_gross_exposure_breach():
    config = PortfolioConfig(
        name="test",
        symbols=["AAPL.US", "TSLA.US"],
        allocations={"AAPL.US": Decimal("0.5"), "TSLA.US": Decimal("0.5")},
        max_gross_exposure=Decimal("0.5"),
    )
    controller = PortfolioRiskController(config)
    prices = {"AAPL.US": Decimal("150"), "TSLA.US": Decimal("200")}
    positions = {"AAPL.US": {"quantity": 10}, "TSLA.US": {"quantity": 10}}
    nav = Decimal("5000")
    events = controller.check(prices, positions, nav, timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc))
    assert any(e.risk_type == "MAX_GROSS_EXPOSURE_BREACH" for e in events)


def test_risk_controller_detects_net_exposure_breach():
    config = PortfolioConfig(
        name="test",
        symbols=["AAPL.US"],
        allocations={"AAPL.US": Decimal("1")},
        max_net_exposure=Decimal("0.1"),
    )
    controller = PortfolioRiskController(config)
    prices = {"AAPL.US": Decimal("150")}
    positions = {"AAPL.US": {"quantity": 10}}
    nav = Decimal("1000")  # net exposure = 1500/1000 = 1.5 > 0.1
    events = controller.check(prices, positions, nav, timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc))
    assert any(e.risk_type == "MAX_NET_EXPOSURE_BREACH" for e in events)


def test_risk_controller_no_breach_when_within_limits():
    config = PortfolioConfig(
        name="test",
        symbols=["AAPL.US"],
        allocations={"AAPL.US": Decimal("1")},
        max_gross_exposure=Decimal("2.0"),
        max_net_exposure=Decimal("2.0"),
    )
    controller = PortfolioRiskController(config)
    prices = {"AAPL.US": Decimal("100")}
    positions = {"AAPL.US": {"quantity": 5}}
    nav = Decimal("1000")  # gross = 500/1000 = 0.5 < 2.0
    events = controller.check(prices, positions, nav, timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc))
    assert events == []


def test_risk_controller_detects_drawdown():
    config = PortfolioConfig(
        name="test",
        symbols=["AAPL.US"],
        allocations={"AAPL.US": Decimal("1")},
    )
    controller = PortfolioRiskController(config)
    ts = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    # Establish peak
    controller.check({"AAPL.US": Decimal("100")}, {"AAPL.US": {"quantity": 100}}, Decimal("10000"), timestamp=ts)
    # NAV drops to 8000 => 20% drawdown > 10%
    events = controller.drawdown(Decimal("8000"), timestamp=ts)
    assert any(e.risk_type == "DRAWDOWN_BREACH" for e in events)
