"""Tests for P343 greeks_surface module and API endpoint."""

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


def test_greeks_surface_call_has_positive_call_delta():
    from app.platform.greeks_surface import greeks_surface_report

    options = [
        {"strike": 100.0, "expiry_days": 30, "iv": 0.2, "option_type": "call"},
        {"strike": 100.0, "expiry_days": 30, "iv": 0.2, "option_type": "put"},
    ]
    result = greeks_surface_report(options, spot=100.0)
    data = result.to_dict()
    greeks_list = data["greeks"]
    assert len(greeks_list) == 2
    call_greeks = greeks_list[0]
    put_greeks = greeks_list[1]
    assert call_greeks["delta"] > 0.0
    assert put_greeks["delta"] < 0.0
    # gamma, vega, theta should be finite
    for g in greeks_list:
        assert abs(g["gamma"]) < float("inf")
        assert g["vega"] > 0.0
        assert abs(g["theta"]) < float("inf")


def test_greeks_surface_summary():
    from app.platform.greeks_surface import greeks_surface_report

    options = [
        {"strike": 100.0, "expiry_days": 30, "iv": 0.2, "option_type": "call"},
    ]
    result = greeks_surface_report(options, spot=100.0)
    data = result.to_dict()
    summary = data["summary"]
    assert "atm_delta" in summary
    assert "total_gamma" in summary
    assert "total_vega" in summary
    assert summary["total_gamma"] > 0.0
    assert summary["total_vega"] > 0.0


def test_greeks_surface_invalid_option_raises():
    from app.platform.greeks_surface import greeks_surface_report

    # missing strike
    try:
        greeks_surface_report([{"expiry_days": 30, "iv": 0.2, "option_type": "call"}], spot=100.0)
        assert False, "should have raised"
    except ValueError:
        pass

    # invalid option_type
    try:
        greeks_surface_report([{"strike": 100.0, "expiry_days": 30, "iv": 0.2, "option_type": "invalid"}], spot=100.0)
        assert False, "should have raised"
    except ValueError:
        pass

    # non-finite spot
    try:
        greeks_surface_report([{"strike": 100.0, "expiry_days": 30, "iv": 0.2, "option_type": "call"}], spot=float("inf"))
        assert False, "should have raised"
    except ValueError:
        pass


def test_greeks_surface_frozen_dataclass():
    from app.platform.greeks_surface import GreeksSurfaceResult

    greeks = [{"strike": 100.0, "expiry": 30.0, "type": "call", "delta": 0.5, "gamma": 0.1, "vega": 0.2, "theta": -0.05}]
    summary = {"atm_delta": 0.5, "total_gamma": 0.1, "total_vega": 0.2}
    result = GreeksSurfaceResult(greeks=greeks, summary=summary)
    d = result.to_dict()
    assert d["greeks"] == greeks
    assert d["summary"] == summary


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


def test_api_greeks_surface_returns_200():
    client = _client_with_mock_auth()
    payload = {
        "options": [
            {"strike": 100.0, "expiry_days": 30, "iv": 0.2, "option_type": "call"},
            {"strike": 100.0, "expiry_days": 30, "iv": 0.2, "option_type": "put"},
        ],
        "spot": 100.0,
        "risk_free": 0.02,
    }
    resp = client.post("/api/platform/greeks-surface", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "greeks" in data
    assert "summary" in data
    assert len(data["greeks"]) == 2


def test_api_greeks_surface_422_on_empty():
    client = _client_with_mock_auth()
    resp = client.post("/api/platform/greeks-surface", json={})
    assert resp.status_code == 422


def test_api_greeks_surface_422_on_missing_spot():
    client = _client_with_mock_auth()
    resp = client.post("/api/platform/greeks-surface", json={"options": []})
    assert resp.status_code == 422


def test_greeks_surface_rejects_float_expiry():
    import pytest
    from app.platform.greeks_surface import greeks_surface_report
    options = [{"strike": 100, "expiry_days": 2.7, "iv": 0.2, "option_type": "call"}]
    with pytest.raises(ValueError):
        greeks_surface_report(options, spot=100)
