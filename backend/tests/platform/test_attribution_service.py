from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.database import engine
from app.models import Base
from app.platform.attribution_service import AttributionService
from app.platform.events import EventSource, FillEvent
from app.platform.portfolio_config import PortfolioConfig
from app.platform.store import EventStore


def _ensure_schema() -> None:
    Base.metadata.create_all(bind=engine)


def _fill(symbol: str, side: str, qty: int, price: str, minute: int, commission: str = "0") -> FillEvent:
    return FillEvent(
        timestamp=datetime(2026, 6, 22, 10, minute, tzinfo=timezone.utc),
        source=EventSource.BROKER,
        symbol=symbol,
        broker_order_id=f"o-{symbol}-{minute}",
        side=side,
        quantity=qty,
        price=Decimal(price),
        commission=Decimal(commission),
    )


def test_attribution_realized_pnl_via_fifo():
    _ensure_schema()
    store = EventStore()
    store.clear()
    config = PortfolioConfig(name="t", symbols=["AAPL.US"], allocations={"AAPL.US": Decimal("1")})
    for f in [
        _fill("AAPL.US", "BUY", 10, "100", 0),
        _fill("AAPL.US", "SELL", 10, "120", 1),
    ]:
        store.append(f)
    result = AttributionService(store).attribute(config, prices={"AAPL.US": Decimal("120")})
    sym = result["per_symbol"]["AAPL.US"]
    assert sym["quantity"] == 0
    assert sym["realized_pnl"] == 200.0
    assert sym["unrealized_pnl"] == 0.0


def test_attribution_unrealized_uses_current_price():
    _ensure_schema()
    store = EventStore()
    store.clear()
    config = PortfolioConfig(name="t", symbols=["AAPL.US"], allocations={"AAPL.US": Decimal("1")})
    store.append(_fill("AAPL.US", "BUY", 10, "100", 0))
    result = AttributionService(store).attribute(config, prices={"AAPL.US": Decimal("130")})
    sym = result["per_symbol"]["AAPL.US"]
    assert sym["quantity"] == 10
    assert sym["avg_cost"] == 100.0
    assert sym["unrealized_pnl"] == 300.0
    assert sym["realized_pnl"] == 0.0


def test_attribution_subtracts_commissions():
    _ensure_schema()
    store = EventStore()
    store.clear()
    config = PortfolioConfig(name="t", symbols=["AAPL.US"], allocations={"AAPL.US": Decimal("1")})
    store.append(_fill("AAPL.US", "BUY", 10, "100", 0, commission="1"))
    store.append(_fill("AAPL.US", "SELL", 10, "120", 1, commission="1"))
    result = AttributionService(store).attribute(config, prices={"AAPL.US": Decimal("120")})
    assert result["per_symbol"]["AAPL.US"]["realized_pnl"] == 198.0  # 200 gross - 2 commissions
