"""P361: systematic risk decomposition tests."""

from __future__ import annotations

import math

import pytest

from app.platform.systematic_risk_decomposition import (
    systematic_risk_decomposition_report,
)


def test_three_asset_panel_basic():
    """Construct a 3-asset panel; systematic_ratio in [0,1], spectrum length=3."""
    n = 50
    a = [0.01 * math.sin(i * 0.3) for i in range(n)]
    b = [0.008 * math.cos(i * 0.35) for i in range(n)]
    c = [0.012 * math.sin(i * 0.4 + 1.0) for i in range(n)]
    panel = {"A": a, "B": b, "C": c}

    result = systematic_risk_decomposition_report(panel)
    assert 0.0 <= result.systematic_ratio <= 1.0
    assert len(result.eigenvalue_spectrum) == 3
    assert len(result.explained_variance_ratio) == 3


def test_concentration_hhi_in_01():
    """HHI should be in [1/n, 1]."""
    n = 50
    panel = {
        f"Asset{i}": [0.01 * math.sin(j * 0.3 + i * 0.1) for j in range(n)]
        for i in range(5)
    }
    result = systematic_risk_decomposition_report(panel)
    n_assets = len(panel)
    assert 1.0 / n_assets <= result.concentration_hhi <= 1.0 + 1e-9


def test_suggested_k_nonzero():
    """suggested_k should be at least 1."""
    n = 60
    panel = {
        "X": [0.01 * math.sin(i * 0.3) for i in range(n)],
        "Y": [0.008 * math.cos(i * 0.25) for i in range(n)],
    }
    result = systematic_risk_decomposition_report(panel)
    assert result.suggested_k >= 1


def test_eigenvalues_descending():
    """eigenvalue_spectrum must be non-increasing."""
    n = 40
    panel = {
        f"Asset{i}": [0.01 * math.sin(j * 0.3 + i * 0.2) for j in range(n)]
        for i in range(4)
    }
    result = systematic_risk_decomposition_report(panel)
    for i in range(1, len(result.eigenvalue_spectrum)):
        assert result.eigenvalue_spectrum[i] <= result.eigenvalue_spectrum[i - 1] + 1e-12


def test_cumulative_explained_ratio_ends_near_1():
    """Cumulative explained variance ratio should end at ~1."""
    n = 40
    panel = {
        "A": [0.01 * math.sin(i * 0.3) for i in range(n)],
        "B": [0.008 * math.cos(i * 0.25) for i in range(n)],
    }
    result = systematic_risk_decomposition_report(panel)
    # last cumulative ratio should be ~1.0
    assert abs(result.explained_variance_ratio[-1] - 1.0) < 1e-9


def test_n_components_clamp():
    """n_components should be clamped to n_assets when provided too large."""
    n = 40
    panel = {
        "A": [0.01 * math.sin(i * 0.3) for i in range(n)],
        "B": [0.008 * math.cos(i * 0.25) for i in range(n)],
    }
    result = systematic_risk_decomposition_report(panel, n_components=5)
    assert len(result.eigenvalue_spectrum) == 2


def test_to_dict_roundtrips():
    """to_dict() returns a plain dict with all expected keys."""
    n = 30
    panel = {
        "A": [0.01 * math.sin(i * 0.3) for i in range(n)],
        "B": [0.008 * math.cos(i * 0.25) for i in range(n)],
    }
    result = systematic_risk_decomposition_report(panel)
    d = result.to_dict()
    assert "systematic_ratio" in d
    assert "eigenvalue_spectrum" in d
    assert "concentration_hhi" in d
    assert "explained_variance_ratio" in d
    assert "suggested_k" in d


def test_empty_panel_raises():
    with pytest.raises(ValueError):
        systematic_risk_decomposition_report({})


def test_single_asset_raises():
    with pytest.raises(ValueError):
        systematic_risk_decomposition_report({"A": [0.01, 0.02]})


def test_unequal_length_raises():
    with pytest.raises(ValueError):
        systematic_risk_decomposition_report(
            {"A": [0.01, 0.02, 0.03], "B": [0.01, 0.02]}
        )


def test_infinite_values_raise():
    with pytest.raises(ValueError):
        systematic_risk_decomposition_report(
            {"A": [0.01, float("inf")], "B": [0.01, 0.02]}
        )


def test_panel_size_limit():
    """Panel of 51 assets should raise ValueError."""
    panel = {f"Asset{i}": [0.01, 0.02] for i in range(51)}
    with pytest.raises(ValueError):
        systematic_risk_decomposition_report(panel)
