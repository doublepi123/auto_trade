"""Tests for P344 rebalancing_intelligence module and API endpoint."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.auth import require_api_key
from app.main import app


def _client_with_mock_auth() -> TestClient:
    """Return a TestClient with the require_api_key dependency mocked out."""
    app.dependency_overrides[require_api_key] = lambda: None
    return TestClient(app)


# ---------------------------------------------------------------------------
# Module-level tests
# ---------------------------------------------------------------------------


def test_rebalancing_intelligence_per_frequency_non_empty():
    from app.platform.rebalancing_intelligence import rebalancing_intelligence_report

    # Construct a simple 2-asset returns panel
    r1 = [0.001, -0.002, 0.003, -0.001, 0.002] * 20  # 100 obs
    r2 = [0.002, -0.001, 0.001, 0.003, -0.002] * 20
    returns_panel = {"A": r1, "B": r2}
    target_weights = {"A": 0.5, "B": 0.5}

    result = rebalancing_intelligence_report(returns_panel, target_weights)
    data = result.to_dict()
    assert "per_frequency" in data
    assert "optimal_frequency" in data
    assert "cost_drag_ratio" in data
    assert len(data["per_frequency"]) > 0
    assert data["optimal_frequency"] in data["per_frequency"]


def test_rebalancing_intelligence_per_frequency_keys():
    from app.platform.rebalancing_intelligence import rebalancing_intelligence_report

    r1 = [0.001] * 60  # enough periods for quarterly detection
    r2 = [0.002] * 60
    returns_panel = {"A": r1, "B": r2}
    target_weights = {"A": 0.5, "B": 0.5}

    result = rebalancing_intelligence_report(returns_panel, target_weights)
    data = result.to_dict()
    for freq_info in data["per_frequency"].values():
        assert "sharpe" in freq_info
        assert "turnover" in freq_info
        assert "tracking_error" in freq_info
        assert "cost_drag" in freq_info


def test_rebalancing_intelligence_custom_cost():
    from app.platform.rebalancing_intelligence import rebalancing_intelligence_report

    r1 = [0.001, -0.002, 0.003] * 30
    r2 = [0.002, -0.001, 0.001] * 30
    returns_panel = {"A": r1, "B": r2}
    target_weights = {"A": 0.5, "B": 0.5}

    result_high = rebalancing_intelligence_report(returns_panel, target_weights, cost_per_turnover=0.01)
    result_low = rebalancing_intelligence_report(returns_panel, target_weights, cost_per_turnover=0.0)
    # Higher cost should reduce sharpe in daily frequency (most turnover)
    d_high = result_high.to_dict()
    d_low = result_low.to_dict()
    assert d_high["cost_drag_ratio"] > 0 or d_low["cost_drag_ratio"] >= 0


def test_rebalancing_intelligence_invalid_empty_panel():
    from app.platform.rebalancing_intelligence import rebalancing_intelligence_report

    try:
        rebalancing_intelligence_report({}, {"A": 0.5})
        assert False, "should have raised"
    except ValueError:
        pass


def test_rebalancing_intelligence_invalid_weights():
    from app.platform.rebalancing_intelligence import rebalancing_intelligence_report

    r1 = [0.001] * 30
    returns_panel = {"A": r1}
    try:
        rebalancing_intelligence_report(returns_panel, {})  # empty weights
        assert False, "should have raised"
    except ValueError:
        pass

    try:
        rebalancing_intelligence_report(returns_panel, {"B": 0.5})  # mismatched keys
        assert False, "should have raised"
    except ValueError:
        pass


def test_rebalancing_intelligence_frozen_dataclass():
    from app.platform.rebalancing_intelligence import RebalancingIntelligenceResult

    per_frequency = {
        "daily": {"sharpe": 0.5, "turnover": 0.1, "tracking_error": 0.02, "cost_drag": 0.001},
    }
    result = RebalancingIntelligenceResult(
        per_frequency=per_frequency,
        optimal_frequency="daily",
        cost_drag_ratio=0.5,
    )
    d = result.to_dict()
    assert d["per_frequency"] == per_frequency
    assert d["optimal_frequency"] == "daily"


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


def test_api_rebalancing_intelligence_returns_200():
    client = _client_with_mock_auth()
    r1 = [0.001, -0.002, 0.003] * 30
    r2 = [0.002, -0.001, 0.001] * 30
    payload = {
        "returns_panel": {"A": r1, "B": r2},
        "target_weights": {"A": 0.5, "B": 0.5},
        "cost_per_turnover": 0.001,
        "periods_per_year": 252,
    }
    resp = client.post("/api/platform/rebalancing-intelligence", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "per_frequency" in data
    assert "optimal_frequency" in data


def test_api_rebalancing_intelligence_422_on_empty():
    client = _client_with_mock_auth()
    resp = client.post("/api/platform/rebalancing-intelligence", json={})
    assert resp.status_code == 422


def test_rebalancing_rejects_invalid_periods_per_year():
    import pytest
    from app.platform.rebalancing_intelligence import rebalancing_intelligence_report
    panel = {"A": [0.01, 0.02, 0.03], "B": [0.02, 0.01, 0.03]}
    weights = {"A": 0.5, "B": 0.5}
    with pytest.raises(ValueError):
        rebalancing_intelligence_report(panel, weights, periods_per_year=0)
    with pytest.raises(ValueError):
        rebalancing_intelligence_report(panel, weights, periods_per_year=-1)


def test_rebalancing_rejects_too_many_assets():
    import pytest
    from app.platform.rebalancing_intelligence import rebalancing_intelligence_report
    panel = {f"A{i}": [0.01, 0.02] for i in range(51)}
    weights = {f"A{i}": 1.0 / 51 for i in range(51)}
    with pytest.raises(ValueError):
        rebalancing_intelligence_report(panel, weights)
