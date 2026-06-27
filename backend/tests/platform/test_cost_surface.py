"""Tests for P311 cost_surface module."""

from __future__ import annotations

import pytest

from app.platform.cost_surface import cost_surface_report


def test_cost_increases_with_participation():
    """Cost should monotonically increase with participation rate."""
    result = cost_surface_report(adv=100000, volatility=0.2)
    d = result.to_dict()

    grid = d["grid"]
    # Group by qty and check cost increases with participation
    by_qty: dict[float, list[dict]] = {}
    for item in grid:
        by_qty.setdefault(item["qty"], []).append(item)

    for qty, items in by_qty.items():
        items_sorted = sorted(items, key=lambda x: x["participation"])
        for i in range(len(items_sorted) - 1):
            assert items_sorted[i + 1]["cost_bps"] >= items_sorted[i]["cost_bps"], (
                f"cost not monotonic in participation for qty={qty}"
            )


def test_cost_increases_with_qty():
    """Cost should monotonically increase with order quantity."""
    result = cost_surface_report(adv=100000, volatility=0.2)
    d = result.to_dict()

    grid = d["grid"]
    # Group by participation and check cost increases with qty
    by_part: dict[float, list[dict]] = {}
    for item in grid:
        by_part.setdefault(item["participation"], []).append(item)

    for part, items in by_part.items():
        items_sorted = sorted(items, key=lambda x: x["qty"])
        for i in range(len(items_sorted) - 1):
            assert items_sorted[i + 1]["cost_bps"] >= items_sorted[i]["cost_bps"], (
                f"cost not monotonic in qty for participation={part}"
            )


def test_cost_surface_grid_size():
    """Default grid should have participation_levels × qty_levels entries."""
    result = cost_surface_report(adv=100000, volatility=0.2)
    d = result.to_dict()
    # Default: 4 participation levels × 4 qty levels = 16 grid points
    assert len(d["grid"]) == 16


def test_cost_surface_custom_levels():
    result = cost_surface_report(
        adv=100000,
        volatility=0.2,
        participation_levels=[0.01, 0.1],
        qty_levels=[500, 5000],
    )
    d = result.to_dict()
    assert len(d["grid"]) == 4


def test_cost_surface_rejects_invalid_adv():
    with pytest.raises(ValueError):
        cost_surface_report(adv=0, volatility=0.2)
    with pytest.raises(ValueError):
        cost_surface_report(adv=-1000, volatility=0.2)


def test_cost_surface_rejects_invalid_volatility():
    with pytest.raises(ValueError):
        cost_surface_report(adv=100000, volatility=-0.1)
    with pytest.raises(ValueError):
        cost_surface_report(adv=100000, volatility=0)


def test_cost_surface_surface_stats():
    result = cost_surface_report(adv=100000, volatility=0.2)
    d = result.to_dict()
    assert "min_cost_bps" in d
    assert "max_cost_bps" in d
    assert "mean_cost_bps" in d
    assert d["min_cost_bps"] <= d["mean_cost_bps"] <= d["max_cost_bps"]


def test_cost_surface_participation_affects_cost():
    body = cost_surface_report(adv=10000, volatility=0.2, participation_levels=[0.01, 0.1], qty_levels=[500.0]).to_dict()
    low = [g for g in body["grid"] if g["participation"] == 0.01][0]["cost_bps"]
    high = [g for g in body["grid"] if g["participation"] == 0.1][0]["cost_bps"]
    assert high != low, "participation must affect cost"
