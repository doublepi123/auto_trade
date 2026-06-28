"""P329: correlation network / MST tests."""

from __future__ import annotations

import pytest


def test_correlation_network_mst_has_n_minus_1_edges():
    """3 assets → MST must have exactly 2 edges, non-empty node_degrees."""
    from app.platform.correlation_network import correlation_network_report

    returns_panel = {
        "A": [0.01, 0.02, -0.01, 0.005, -0.005],
        "B": [0.02, 0.04, -0.02, 0.01, -0.01],
        "C": [-0.005, -0.01, 0.005, -0.005, 0.01],
    }
    result = correlation_network_report(returns_panel, method="pearson").to_dict()
    assert len(result["mst_edges"]) == 2
    assert len(result["node_degrees"]) == 3
    assert all(d > 0 for d in result["node_degrees"].values())
    assert result["average_distance"] > 0


def test_correlation_network_spearman():
    """method='spearman' should also produce a valid MST."""
    from app.platform.correlation_network import correlation_network_report

    returns_panel = {
        "X": [0.01, -0.02, 0.03, -0.01, 0.0],
        "Y": [0.02, -0.04, 0.06, -0.02, 0.01],
        "Z": [-0.01, 0.01, -0.02, 0.02, -0.005],
    }
    result = correlation_network_report(returns_panel, method="spearman").to_dict()
    assert len(result["mst_edges"]) == 2


def test_correlation_network_rejects_invalid_method():
    from app.platform.correlation_network import correlation_network_report

    with pytest.raises(ValueError, match="method must be"):
        correlation_network_report({"A": [0.01]}, method="kendall")


def test_correlation_network_rejects_small_panel():
    from app.platform.correlation_network import correlation_network_report

    with pytest.raises(ValueError, match="at least 2"):
        correlation_network_report({"A": [0.01, 0.02]}, method="pearson")


def test_correlation_network_rejects_too_large_panel():
    from app.platform.correlation_network import correlation_network_report

    big = {f"A{i}": [0.01] * 10 for i in range(51)}
    with pytest.raises(ValueError, match="at most 50"):
        correlation_network_report(big, method="pearson")
