"""Tests for P351 multi-asset Kelly portfolio allocation."""

from __future__ import annotations

import math

import pytest

from app.platform.multi_asset_kelly import (
    MultiAssetKellyResult,
    multi_asset_kelly_report,
)


def test_two_asset_kelly_positive_growth():
    """Two-asset panel with positive expected returns: kelly_weights non-empty, growth > 0."""
    # Asset A: mean ~0.001 (positive), Asset B: mean ~0.0005 (positive)
    panel = {
        "A": [0.002, -0.001, 0.003, 0.000, 0.001, -0.002, 0.004, 0.001],
        "B": [0.001, 0.000, 0.002, -0.001, 0.001, 0.000, 0.002, -0.001],
    }
    result = multi_asset_kelly_report(panel, fraction=1.0)
    assert isinstance(result, MultiAssetKellyResult)
    assert len(result.kelly_weights) == 2
    assert len(result.fractional_weights) == 2
    assert result.expected_growth_rate > 0.0
    assert result.leverage > 0.0


def test_half_kelly_lower_leverage():
    """Half Kelly should have roughly half the leverage of full Kelly."""
    panel = {
        "A": [0.002, -0.001, 0.003, 0.000, 0.001, -0.002, 0.004, 0.001],
        "B": [0.001, 0.000, 0.002, -0.001, 0.001, 0.000, 0.002, -0.001],
    }
    full = multi_asset_kelly_report(panel, fraction=1.0)
    half = multi_asset_kelly_report(panel, fraction=0.5)
    assert half.leverage > 0.0
    assert half.leverage < full.leverage
    # fractional weights should be approx 0.5 * full
    for sym in panel:
        assert abs(half.fractional_weights.get(sym, 0.0) - 0.5 * full.kelly_weights.get(sym, 0.0)) < 1e-9


def test_kelly_weights_sum_can_exceed_one():
    """Kelly weights can sum to >1 (leverage)."""
    panel = {
        "A": [0.005, 0.002, 0.008, 0.001, 0.003],
        "B": [0.003, 0.001, 0.006, 0.002, 0.004],
    }
    result = multi_asset_kelly_report(panel, fraction=1.0)
    assert result.leverage > 0.0


def test_zero_leverage_when_zero_returns():
    """Zero-variance panel gives zero leverage."""
    panel = {
        "A": [0.0, 0.0, 0.0, 0.0],
        "B": [0.0, 0.0, 0.0, 0.0],
    }
    result = multi_asset_kelly_report(panel, fraction=1.0)
    assert result.leverage == 0.0


def test_fraction_bounds():
    """Fraction must be in (0, 1]."""
    panel = {"A": [0.01, -0.01]}
    with pytest.raises(ValueError):
        multi_asset_kelly_report(panel, fraction=0.0)
    with pytest.raises(ValueError):
        multi_asset_kelly_report(panel, fraction=-0.1)
    with pytest.raises(ValueError):
        multi_asset_kelly_report(panel, fraction=1.5)


def test_empty_panel():
    with pytest.raises(ValueError):
        multi_asset_kelly_report({}, fraction=1.0)


def test_single_series_too_short():
    with pytest.raises(ValueError):
        multi_asset_kelly_report({"A": [0.01]}, fraction=1.0)


def test_unequal_lengths():
    with pytest.raises(ValueError):
        multi_asset_kelly_report({"A": [0.01, 0.02], "B": [0.01]}, fraction=1.0)


def test_non_finite_returns():
    with pytest.raises(ValueError):
        multi_asset_kelly_report({"A": [float("nan"), 0.01]}, fraction=1.0)


def test_result_to_dict():
    panel = {
        "A": [0.002, -0.001, 0.003, 0.000, 0.001],
        "B": [0.001, 0.000, 0.002, -0.001, 0.001],
    }
    result = multi_asset_kelly_report(panel, fraction=0.75)
    d = result.to_dict()
    assert isinstance(d, dict)
    assert isinstance(d["kelly_weights"], dict)
    assert isinstance(d["fractional_weights"], dict)
    assert isinstance(d["expected_growth_rate"], float)
    assert isinstance(d["leverage"], float)
    assert d["fraction"] == 0.75


def test_multi_asset_kelly_rejects_too_many_assets():
    import pytest
    from app.platform.multi_asset_kelly import multi_asset_kelly_report
    panel = {f"A{i}": [0.01, 0.02, 0.03] for i in range(51)}
    with pytest.raises(ValueError):
        multi_asset_kelly_report(panel)
