"""Tests for the P199 Transaction Cost Analysis (TCA) analyzer."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.database import engine, init_db
from app.models import Base, Transaction
from app.platform.tca import (
    ConstReferencePriceProvider,
    TcaAnalyzer,
    TcaFill,
)


def _setup() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    init_db()


def _fill(side: str, qty: int, price: str, ref: str, minute: int, symbol: str = "A.US") -> TcaFill:
    return TcaFill(
        broker_order_id=f"o-{minute}",
        symbol=symbol,
        side=side,
        quantity=qty,
        price=Decimal(price),
        commission=Decimal("1.0"),
        reference=Decimal(ref),
        timestamp=datetime(2026, 6, 24, 10, minute, tzinfo=timezone.utc),
        source="paper",
    )


def test_signed_slippage_buy_paid_above_reference_is_unfavorable():
    fill = _fill("BUY", 100, "101", "100", 0)
    assert fill.signed_slippage == Decimal("1")
    assert fill.slippage_cost == Decimal("100")  # 1 * 100
    assert fill.total_cost == Decimal("101")  # 100 slippage + 1 commission


def test_signed_slippage_sell_below_reference_is_unfavorable():
    # SELL at 99 vs reference 100 -> unfavorable by 1
    fill = _fill("SELL", 100, "99", "100", 0)
    assert fill.signed_slippage == Decimal("1")
    assert fill.slippage_cost == Decimal("100")


def test_signed_slippage_favorable_fill_is_negative_cost():
    # BUY at 99 vs reference 100 -> favorable: signed slippage -1, cost -100 (rebate).
    fill = _fill("BUY", 100, "99", "100", 0)
    assert fill.signed_slippage == Decimal("-1")
    assert fill.slippage_cost == Decimal("-100")
    assert fill.total_cost == Decimal("-99")  # -100 slippage + 1 commission


def test_no_reference_means_zero_slippage():
    fill = TcaFill(
        broker_order_id="o", symbol="A.US", side="BUY", quantity=10,
        price=Decimal("100"), commission=Decimal("1"),
        reference=None, timestamp=datetime(2026, 6, 24, tzinfo=timezone.utc),
    )
    assert fill.signed_slippage == Decimal("0")
    assert fill.total_cost == Decimal("1")


def test_attribute_by_symbol_and_side():
    fills = [
        _fill("BUY", 100, "101", "100", 0, symbol="A.US"),
        _fill("SELL", 50, "99", "100", 1, symbol="B.US"),
    ]
    attr = TcaAnalyzer(reference_provider=ConstReferencePriceProvider({})).analyze_fills(fills)

    assert "A.US" in attr.by_symbol
    assert "B.US" in attr.by_symbol
    assert attr.by_symbol["A.US"]["slippage_cost"] == Decimal("100")
    # A.US: 100 shares unfavorable; B.US: 50 shares unfavorable at 1 slip.
    assert attr.by_side["BUY"]["slippage_cost"] == Decimal("100")
    assert attr.by_side["SELL"]["slippage_cost"] == Decimal("50")


def test_totals_aggregate_costs():
    fills = [
        _fill("BUY", 100, "101", "100", 0),  # slip 100, comm 1
        _fill("SELL", 50, "99", "100", 1),   # slip 50, comm 1
    ]
    attr = TcaAnalyzer(reference_provider=ConstReferencePriceProvider({})).analyze_fills(fills)

    assert attr.totals["fills"] == Decimal("2")
    assert attr.totals["quantity"] == Decimal("150")
    assert attr.totals["slippage_cost"] == Decimal("150")
    assert attr.totals["commission"] == Decimal("2")
    assert attr.totals["total_cost"] == Decimal("152")


def test_avg_slippage_bps_computed_against_notional():
    fills = [_fill("BUY", 100, "101", "100", 0)]  # notional 100*101 = 10100, slip 100
    attr = TcaAnalyzer(reference_provider=ConstReferencePriceProvider({})).analyze_fills(fills)
    expected = Decimal("100") / Decimal("10100") * Decimal("10000")
    assert abs(attr.totals["avg_slippage_bps"] - expected) < Decimal("1e-6")


def test_time_bucketing_by_day():
    day1 = datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc)
    fill1 = TcaFill(broker_order_id="o1", symbol="A.US", side="BUY", quantity=10,
                    price=Decimal("101"), commission=Decimal("1"),
                    reference=Decimal("100"), timestamp=day1)
    fill2 = TcaFill(broker_order_id="o2", symbol="A.US", side="BUY", quantity=10,
                    price=Decimal("101"), commission=Decimal("1"),
                    reference=Decimal("100"), timestamp=day2)
    attr = TcaAnalyzer(reference_provider=ConstReferencePriceProvider({})).analyze_fills([fill1, fill2])

    assert set(attr.by_bucket.keys()) == {"2026-06-24", "2026-06-25"}


def test_analyze_reads_from_transaction_ledger():
    _setup()
    with Session(engine) as db:
        db.add(Transaction(broker_order_id="o1", symbol="A.US", side="BUY",
                           quantity=100, price=101.0, commission=1.0,
                           source="paper",
                           timestamp=datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc)))
        db.commit()
        provider = ConstReferencePriceProvider({"A.US": Decimal("100")})
        analyzer = TcaAnalyzer(reference_provider=provider, db=db)
        attr = analyzer.analyze(symbol="A.US")

    assert attr.totals["fills"] == Decimal("1")
    assert attr.by_symbol["A.US"]["slippage_cost"] == Decimal("100")


def test_to_dict_serializes_to_floats():
    fills = [_fill("BUY", 100, "101", "100", 0)]
    attr = TcaAnalyzer(reference_provider=ConstReferencePriceProvider({})).analyze_fills(fills)
    out = TcaAnalyzer.to_dict(attr)

    assert isinstance(out["totals"]["total_cost"], float)
    assert out["totals"]["total_cost"] == 101.0
    assert isinstance(out["by_symbol"]["A.US"]["slippage_cost"], float)
