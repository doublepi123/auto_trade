"""P341: Granger network tests."""

from __future__ import annotations

import pytest


def test_granger_network_basic():
    """3 assets → adjacency matrix 3x3, pagerank non-empty."""
    from app.platform.granger_network import granger_network_report

    returns_panel = {
        "A": [0.01, 0.02, -0.01, 0.005, -0.005, 0.01, -0.02, 0.015, 0.0, 0.005,
              0.02, 0.03, -0.015, 0.01, -0.01],
        "B": [0.02, 0.04, -0.02, 0.01, -0.01, 0.02, -0.04, 0.03, 0.0, 0.01,
              0.04, 0.06, -0.03, 0.02, -0.02],
        "C": [-0.005, -0.01, 0.005, -0.005, 0.01, -0.015, 0.02, -0.01, 0.005, 0.0,
              -0.01, -0.02, 0.01, -0.01, 0.02],
    }
    result = granger_network_report(returns_panel, max_lag=2, significance=0.1)
    d = result.to_dict()
    assert "adjacency_matrix" in d
    assert "significant_edges" in d
    assert "pagerank" in d
    assert "strongly_connected_components" in d

    adj = d["adjacency_matrix"]
    assert isinstance(adj, dict)
    assert len(adj) == 3
    for row in adj.values():
        assert isinstance(row, dict)
        assert len(row) == 3

    pr = d["pagerank"]
    assert isinstance(pr, dict)
    assert len(pr) == 3
    assert all(v > 0 for v in pr.values())


def test_granger_network_rejects_empty_panel():
    from app.platform.granger_network import granger_network_report

    with pytest.raises(ValueError):
        granger_network_report({})


def test_granger_network_rejects_single_asset():
    from app.platform.granger_network import granger_network_report

    with pytest.raises(ValueError):
        granger_network_report({"A": [0.01, 0.02, 0.03]})


def test_granger_network_rejects_unequal_length():
    from app.platform.granger_network import granger_network_report

    with pytest.raises(ValueError):
        granger_network_report({"A": [0.01, 0.02], "B": [0.01]})


def test_granger_network_rejects_nan():
    from app.platform.granger_network import granger_network_report

    with pytest.raises(ValueError):
        granger_network_report({"A": [0.01, float("nan")], "B": [0.01, 0.02]})
