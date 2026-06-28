"""Tests for P332–P334 API endpoints."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.auth import require_api_key
from app.main import app


def _client_with_mock_auth() -> TestClient:
    """Return a TestClient with the require_api_key dependency mocked out."""
    app.dependency_overrides[require_api_key] = lambda: None
    return TestClient(app)


def test_reverse_stress_endpoint_returns_200():
    client = _client_with_mock_auth()
    payload = {
        "positions": {"AAPL": 10000, "GOOG": 5000},
        "betas": {"AAPL": 1.2, "GOOG": 1.0},
        "loss_threshold": 2000,
    }
    resp = client.post("/api/platform/reverse-stress", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "critical_scenario_name" in data
    assert "critical_multiplier" in data
    assert data["critical_multiplier"] > 0


def test_reverse_stress_endpoint_422_on_invalid():
    client = _client_with_mock_auth()
    resp = client.post("/api/platform/reverse-stress", json={})
    assert resp.status_code == 422


def test_dynamic_style_analysis_endpoint_returns_200():
    client = _client_with_mock_auth()
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01] * 5
    f2 = [0.0, -0.01, 0.02, 0.01, -0.03, 0.02, 0.0, 0.01] * 5
    returns = [0.8 * f1[i] + 0.2 * f2[i] for i in range(40)]
    payload = {
        "returns": returns,
        "factor_returns": {"F1": f1, "F2": f2},
        "window": 10,
        "constraint": "sum_eq_one",
    }
    resp = client.post("/api/platform/dynamic-style-analysis", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "per_window_weights" in data
    assert "r_squared_series" in data
    assert "style_drift_score" in data
    assert "drift_detected" in data


def test_dynamic_style_analysis_endpoint_422_on_empty():
    client = _client_with_mock_auth()
    resp = client.post("/api/platform/dynamic-style-analysis", json={})
    assert resp.status_code == 422


def test_online_covariance_endpoint_returns_200():
    client = _client_with_mock_auth()
    r1 = [0.01, -0.02, 0.005, 0.01, -0.01] * 10
    r2 = [0.005, -0.01, 0.01, 0.015, -0.005] * 10
    payload = {
        "returns_panel": {"A": r1, "B": r2},
        "lam": 0.97,
        "min_window": 5,
    }
    resp = client.post("/api/platform/online-covariance", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "latest_covariance" in data
    assert "condition_number" in data
    assert "eigenvalues" in data
    assert "assets" in data
    assert data["condition_number"] > 0


def test_online_covariance_endpoint_422_on_empty():
    client = _client_with_mock_auth()
    resp = client.post("/api/platform/online-covariance", json={})
    assert resp.status_code == 422
