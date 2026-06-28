"""Tests for P345 dynamic_risk_contribution module and API endpoint."""

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


def test_dynamic_risk_contribution_contributions_have_series():
    from app.platform.dynamic_risk_contribution import dynamic_risk_contribution_report

    # 2-asset panel with 50 obs, window=20 → should produce ~31 windows
    r1 = [0.01, -0.02, 0.005, 0.01, -0.01] * 10  # 50 obs
    r2 = [0.005, -0.01, 0.01, 0.015, -0.005] * 10
    returns_panel = {"A": r1, "B": r2}
    weights = {"A": 0.4, "B": 0.6}

    result = dynamic_risk_contribution_report(returns_panel, weights, window=20)
    data = result.to_dict()
    contributions = data["contributions"]
    assert "A" in contributions
    assert "B" in contributions
    assert len(contributions["A"]) > 0
    assert len(contributions["B"]) > 0
    assert len(contributions["A"]) == len(contributions["B"])


def test_dynamic_risk_contribution_summary_non_empty():
    from app.platform.dynamic_risk_contribution import dynamic_risk_contribution_report

    r1 = [0.01, -0.02, 0.005] * 20  # 60 obs
    r2 = [0.005, -0.01, 0.01] * 20
    returns_panel = {"A": r1, "B": r2}
    weights = {"A": 0.5, "B": 0.5}

    result = dynamic_risk_contribution_report(returns_panel, weights, window=20)
    data = result.to_dict()
    assert "summary" in data
    summary = data["summary"]
    assert "A" in summary
    assert "B" in summary
    # Each summary should have avg_contribution_pct
    assert summary["A"]["avg_contribution_pct"] > 0
    assert summary["B"]["avg_contribution_pct"] > 0


def test_dynamic_risk_contribution_drift_flags():
    from app.platform.dynamic_risk_contribution import dynamic_risk_contribution_report

    r1 = [0.01, -0.02, 0.005] * 20
    r2 = [0.005, -0.01, 0.01] * 20
    returns_panel = {"A": r1, "B": r2}
    weights = {"A": 0.5, "B": 0.5}

    result = dynamic_risk_contribution_report(returns_panel, weights, window=20)
    data = result.to_dict()
    assert "drift_flags" in data
    assert "A" in data["drift_flags"]
    assert "B" in data["drift_flags"]
    assert isinstance(data["drift_flags"]["A"], bool)
    assert isinstance(data["drift_flags"]["B"], bool)


def test_dynamic_risk_contribution_invalid_empty_panel():
    from app.platform.dynamic_risk_contribution import dynamic_risk_contribution_report

    try:
        dynamic_risk_contribution_report({}, {"A": 0.5})
        assert False, "should have raised"
    except ValueError:
        pass


def test_dynamic_risk_contribution_invalid_weights():
    from app.platform.dynamic_risk_contribution import dynamic_risk_contribution_report

    r1 = [0.01] * 30
    returns_panel = {"A": r1}
    try:
        dynamic_risk_contribution_report(returns_panel, {})  # empty weights
        assert False, "should have raised"
    except ValueError:
        pass


def test_dynamic_risk_contribution_panel_size_limit():
    from app.platform.dynamic_risk_contribution import dynamic_risk_contribution_report

    # Panel with 51 assets should raise
    returns_panel = {f"A{i}": [0.01] * 30 for i in range(51)}
    weights = {f"A{i}": 1.0 / 51 for i in range(51)}
    try:
        dynamic_risk_contribution_report(returns_panel, weights, window=20)
        assert False, "should have raised"
    except ValueError:
        pass


def test_dynamic_risk_contribution_frozen_dataclass():
    from app.platform.dynamic_risk_contribution import DynamicRiskContributionResult

    contributions = {"A": [0.4, 0.42, 0.38], "B": [0.6, 0.58, 0.62]}
    drift_flags = {"A": False, "B": False}
    summary = {"A": {"avg_contribution_pct": 0.4}, "B": {"avg_contribution_pct": 0.6}}
    result = DynamicRiskContributionResult(
        contributions=contributions,
        drift_flags=drift_flags,
        summary=summary,
    )
    d = result.to_dict()
    assert d["contributions"] == contributions
    assert d["drift_flags"] == drift_flags
    assert d["summary"] == summary


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


def test_api_dynamic_risk_contribution_returns_200():
    client = _client_with_mock_auth()
    r1 = [0.01, -0.02, 0.005] * 20
    r2 = [0.005, -0.01, 0.01] * 20
    payload = {
        "returns_panel": {"A": r1, "B": r2},
        "weights": {"A": 0.4, "B": 0.6},
        "window": 20,
        "periods_per_year": 252,
    }
    resp = client.post("/api/platform/dynamic-risk-contribution", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "contributions" in data
    assert "drift_flags" in data
    assert "summary" in data


def test_api_dynamic_risk_contribution_422_on_empty():
    client = _client_with_mock_auth()
    resp = client.post("/api/platform/dynamic-risk-contribution", json={})
    assert resp.status_code == 422
