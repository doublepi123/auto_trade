from __future__ import annotations

from decimal import Decimal

import pytest

from app.platform.execution_algorithms import (
    IcebergAlgorithm,
    TWAPAlgorithm,
    VWAPAlgorithm,
    get_default_registry,
)
from app.platform.sdk import OrderIntent


def _parent(qty: int) -> OrderIntent:
    return OrderIntent(symbol="A", side="BUY", quantity=qty, order_type="LIMIT", limit_price=Decimal("100"), reason="parent")


def test_twap_equal_split_sums_to_parent():
    slices = TWAPAlgorithm(num_slices=4).slice(_parent(100))
    assert len(slices) == 4
    assert sum(s.quantity for s in slices) == 100
    # remainder distributed to the first slots (100/4 = 25 each, no remainder)
    assert all(s.quantity == 25 for s in slices)


def test_twap_remainder_distributed():
    slices = TWAPAlgorithm(num_slices=3).slice(_parent(100))
    assert sum(s.quantity for s in slices) == 100
    assert sorted(s.quantity for s in slices) == [33, 33, 34]


def test_vwap_follows_volume_profile():
    slices = VWAPAlgorithm(volume_profile=(1.0, 3.0)).slice(_parent(100))
    assert sum(s.quantity for s in slices) == 100
    # slot 2 has 3x weight -> 75 vs 25
    assert slices[0].quantity == 25
    assert slices[1].quantity == 75


def test_iceberg_chunks_display_quantity():
    slices = IcebergAlgorithm(display_quantity=30).slice(_parent(100))
    assert [s.quantity for s in slices] == [30, 30, 30, 10]


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        TWAPAlgorithm(num_slices=0).slice(_parent(10))
    with pytest.raises(ValueError):
        VWAPAlgorithm(volume_profile=()).slice(_parent(10))
    with pytest.raises(ValueError):
        IcebergAlgorithm(display_quantity=0).slice(_parent(10))


def test_registry_lists_defaults():
    reg = get_default_registry()
    names = {a["name"] for a in reg.list()}
    assert names == {"twap", "vwap", "iceberg"}
    assert reg.get("twap").slice(_parent(4))


def test_children_inherit_symbol_side_order_type():
    slices = TWAPAlgorithm(num_slices=2).slice(_parent(10))
    for s in slices:
        assert s.symbol == "A"
        assert s.side == "BUY"
        assert s.order_type == "LIMIT"
        assert s.limit_price == Decimal("100")
