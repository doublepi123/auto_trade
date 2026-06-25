"""Tests for P219 implementation shortfall TCA."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.platform.shortfall import (
    ShortfallAnalyzer,
    ShortfallFill,
    ShortfallOrder,
    implementation_shortfall,
    shortfall_from_tca,
)
from app.platform.tca import ConstReferencePriceProvider, TcaFill


def test_buy_filled_above_arrival_realized_cost():
    order = ShortfallOrder(symbol="A.US", side="BUY", ordered_quantity=Decimal("100"),
                           arrival_price=Decimal("100"))
    fills = [ShortfallFill(quantity=Decimal("100"), price=Decimal("101"))]
    b = implementation_shortfall(order, fills)
    assert b.realized_cost == Decimal("100")
    assert b.fees == Decimal("0")
    assert b.opportunity_cost == Decimal("0")
    assert b.total_shortfall == Decimal("100")
    assert b.realized_bps == Decimal("100")
    assert b.participation_rate == Decimal("1")
    assert b.vwap == Decimal("101")


def test_sell_filled_below_arrival_realized_cost_positive():
    order = ShortfallOrder(symbol="A.US", side="SELL", ordered_quantity=Decimal("100"),
                           arrival_price=Decimal("100"))
    fills = [ShortfallFill(quantity=Decimal("100"), price=Decimal("99"))]
    b = implementation_shortfall(order, fills)
    # sign=-1: realized = -1 * (99-100) * 100 = +100 (loss vs decision)
    assert b.realized_cost == Decimal("100")
    assert b.total_shortfall == Decimal("100")


def test_partial_fill_opportunity_cost_rises_with_unfilled():
    order = ShortfallOrder(symbol="A.US", side="BUY", ordered_quantity=Decimal("100"),
                           arrival_price=Decimal("100"), benchmark="close",
                           benchmark_price=Decimal("110"))
    fills = [ShortfallFill(quantity=Decimal("60"), price=Decimal("100"))]
    b = implementation_shortfall(order, fills)
    assert b.realized_cost == Decimal("0")
    # opportunity = +1 * (110-100) * 40 = +400
    assert b.opportunity_cost == Decimal("400")
    assert b.unfilled_quantity == Decimal("40")
    assert b.participation_rate == Decimal("0.6")


def test_arrival_benchmark_zero_timing_and_opportunity_when_unfilled():
    order = ShortfallOrder(symbol="A.US", side="BUY", ordered_quantity=Decimal("100"),
                           arrival_price=Decimal("100"), benchmark="arrival")
    fills = [ShortfallFill(quantity=Decimal("60"), price=Decimal("102"))]
    b = implementation_shortfall(order, fills)
    assert b.benchmark_price == Decimal("100")
    assert b.realized_cost == Decimal("120")  # +1*(102-100)*60
    assert b.opportunity_cost == Decimal("0")
    assert b.timing_cost == Decimal("0")
    assert b.total_shortfall == Decimal("120")


def test_vwap_benchmark_uses_computed_vwap():
    order = ShortfallOrder(symbol="A.US", side="BUY", ordered_quantity=Decimal("100"),
                           arrival_price=Decimal("100"), benchmark="vwap")
    fills = [ShortfallFill(quantity=Decimal("50"), price=Decimal("101")),
            ShortfallFill(quantity=Decimal("50"), price=Decimal("103"))]
    b = implementation_shortfall(order, fills)
    assert b.vwap == Decimal("102")
    assert b.benchmark_price == Decimal("102")
    # realized (vs arrival) = +1*(102-100)*100 = 200 ; timing = +1*(102-100)*100 = 200
    assert b.realized_cost == Decimal("200")
    assert b.timing_cost == Decimal("200")


def test_close_benchmark_requires_price():
    order = ShortfallOrder(symbol="A.US", side="BUY", ordered_quantity=Decimal("100"),
                           arrival_price=Decimal("100"), benchmark="close")
    with pytest.raises(ValueError):
        implementation_shortfall(order, [ShortfallFill(quantity=Decimal("100"), price=Decimal("100"))])


def test_fees_summed_from_fills_when_order_fees_zero():
    order = ShortfallOrder(symbol="A.US", side="BUY", ordered_quantity=Decimal("100"),
                           arrival_price=Decimal("100"))
    fills = [ShortfallFill(quantity=Decimal("100"), price=Decimal("101"), commission=Decimal("2"))]
    b = implementation_shortfall(order, fills)
    assert b.fees == Decimal("2")


def test_explicit_order_fees_overrides_fill_commission():
    order = ShortfallOrder(symbol="A.US", side="BUY", ordered_quantity=Decimal("100"),
                           arrival_price=Decimal("100"), fees=Decimal("5"))
    fills = [ShortfallFill(quantity=Decimal("100"), price=Decimal("101"), commission=Decimal("2"))]
    b = implementation_shortfall(order, fills)
    assert b.fees == Decimal("5")


def test_empty_fills_full_opportunity():
    order = ShortfallOrder(symbol="A.US", side="BUY", ordered_quantity=Decimal("100"),
                           arrival_price=Decimal("100"), benchmark="close",
                           benchmark_price=Decimal("110"))
    b = implementation_shortfall(order, [])
    assert b.filled_quantity == Decimal("0")
    assert b.vwap is None
    assert b.realized_cost == Decimal("0")
    assert b.opportunity_cost == Decimal("1000")  # +1*(110-100)*100
    assert b.participation_rate == Decimal("0")


def test_unknown_side_raises():
    order = ShortfallOrder(symbol="A.US", side="HOLD", ordered_quantity=Decimal("100"),
                           arrival_price=Decimal("100"))
    with pytest.raises(ValueError):
        implementation_shortfall(order, [])


def test_missing_arrival_raises():
    order = ShortfallOrder(symbol="A.US", side="BUY", ordered_quantity=Decimal("100"))
    with pytest.raises(ValueError):
        implementation_shortfall(order, [])


def test_shortfall_from_tca_reuses_tca_fill():
    tf = TcaFill(broker_order_id="o1", symbol="A.US", side="BUY", quantity=100,
                 price=Decimal("101"), commission=Decimal("1"), reference=Decimal("100"),
                 timestamp=datetime(2024, 1, 1, 9, 30))
    order = ShortfallOrder(symbol="A.US", side="BUY", ordered_quantity=Decimal("100"))
    b = shortfall_from_tca(order, [tf])
    assert b.arrival_price == Decimal("100")
    assert b.realized_cost == Decimal("100")
    assert b.fees == Decimal("1")
    assert b.total_shortfall == Decimal("101")


def test_total_bps_against_ordered_notional():
    order = ShortfallOrder(symbol="A.US", side="BUY", ordered_quantity=Decimal("200"),
                           arrival_price=Decimal("50"))
    fills = [ShortfallFill(quantity=Decimal("100"), price=Decimal("51"))]
    b = implementation_shortfall(order, fills)
    # total = +1*(51-50)*100 = 100 ; total_bps = 100/(50*200)*1e4 = 100
    assert b.total_shortfall == Decimal("100")
    assert b.total_bps == Decimal("100")


def test_to_dict_serializes_floats():
    order = ShortfallOrder(symbol="A.US", side="BUY", ordered_quantity=Decimal("100"),
                           arrival_price=Decimal("100"))
    b = implementation_shortfall(order, [ShortfallFill(quantity=Decimal("100"), price=Decimal("101"))])
    d = b.to_dict()
    assert isinstance(d["realized_cost"], float)
    assert isinstance(d["vwap"], float)
    assert d["vwap"] == 101.0