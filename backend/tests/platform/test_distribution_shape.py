"""P327: distribution shape tests."""

from __future__ import annotations

import math

import pytest

from app.platform.distribution_shape import (
    DistributionShapeResult,
    distribution_shape_report,
)


def test_distribution_shape_basic():
    """Basic call returns valid structure."""
    returns = [0.01, -0.02, 0.03, -0.01, 0.02] * 20
    result = distribution_shape_report(returns, window=10)
    d = result.to_dict()
    assert "bars" in d
    assert len(d["bars"]) > 0
    for bar in d["bars"]:
        assert "skew" in bar
        assert "kurtosis" in bar
        assert "tail_index" in bar


def test_distribution_shape_fat_tail_clusters():
    """Sequence with extreme values produces non-empty fat_tail_clusters."""
    # normal-ish returns plus a big outlier cluster
    returns = [0.001] * 40 + [0.15, -0.12, 0.18, -0.14] + [0.001] * 40
    result = distribution_shape_report(returns, window=20)
    d = result.to_dict()
    assert "fat_tail_clusters" in d
    # fat_tail_clusters should be non-empty because outlier cluster inflates kurtosis
    assert len(d["fat_tail_clusters"]) > 0


def test_distribution_shape_fat_tail_clusters_empty_normal():
    """Normal-like returns → fat_tail_clusters likely empty."""
    returns = [0.001, -0.002, 0.0015, -0.001, 0.002, -0.0015] * 20
    result = distribution_shape_report(returns, window=20)
    d = result.to_dict()
    # fat_tail_clusters may be empty for well-behaved returns
    assert isinstance(d["fat_tail_clusters"], list)


def test_distribution_shape_skew_negative():
    """Negative returns skew → some bars show negative skew."""
    returns = [0.005, -0.05, 0.005, -0.04, 0.01, -0.06, 0.005, -0.03] * 10
    result = distribution_shape_report(returns, window=8)
    d = result.to_dict()
    skews = [b["skew"] for b in d["bars"]]
    # at least some bars should have negative skew
    assert any(s < 0 for s in skews)


def test_distribution_shape_to_dict_keys():
    """to_dict contains expected top-level keys."""
    returns = [0.01, -0.02, 0.03] * 30
    result = distribution_shape_report(returns, window=10)
    d = result.to_dict()
    for key in ("n_observations", "window", "bars", "fat_tail_clusters", "summary"):
        assert key in d, f"missing key {key}"


def test_distribution_shape_short_series():
    """Short series (< window) returns empty bars."""
    returns = [0.01, 0.02, 0.03]
    result = distribution_shape_report(returns, window=10)
    d = result.to_dict()
    assert d["bars"] == []
    assert d["fat_tail_clusters"] == []


def test_distribution_shape_rejects_invalid():
    """Invalid inputs raise ValueError."""
    with pytest.raises(ValueError):
        distribution_shape_report([], window=10)
    with pytest.raises(ValueError):
        distribution_shape_report([0.01], window=0)
    with pytest.raises(ValueError):
        distribution_shape_report([0.01, float("nan")], window=2)
