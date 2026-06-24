"""Tests for P211 / P212 risk-metrics and portfolio-optimize endpoints."""

from __future__ import annotations

import os

# Set test DB and credential key BEFORE app imports.
os.environ.setdefault("AUTO_TRADE_DATABASE_URL", "sqlite:///./data/test_p211_p212.db")
os.environ.setdefault("AUTO_TRADE_ENV", "test")
os.environ.setdefault("AUTO_TRADE_CREDENTIAL_KEY_PATH", "./data/test_cred_p211.pem")
os.environ.setdefault("AUTO_TRADE_API_KEY", "test-key")

import sys

# Force the worktree to be the first source for `app.*` so the new endpoints
# (added in this iteration) are actually loaded. The main backend path is
# appended for shared utilities (FastAPI, etc.) only.
WORKTREE = "/Users/lcy/code/auto_trade/.claude/worktrees/p203-p212-risk-science/backend"
if WORKTREE in sys.path:
    sys.path.remove(WORKTREE)
sys.path.insert(0, WORKTREE)

# Remove cached modules so they re-import from the worktree
for k in list(sys.modules.keys()):
    if k == "app" or k.startswith("app."):
        del sys.modules[k]

from fastapi.testclient import TestClient

from app.main import app


def _request():
    return TestClient(app, headers={"X-API-Key": "test-key"})


def test_risk_metrics_endpoint_with_returns():
    payload = {
        "returns": [0.01, -0.02, 0.03, -0.005, 0.015, -0.01, 0.025, -0.015, 0.02, -0.01, 0.005, 0.0],
        "confidence_levels": [0.95],
    }
    client = _request()
    r = client.post("/api/platform/risk-metrics", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "var" in body and "drawdown" in body and "pain" in body and "tail" in body and "ratios" in body
    # risk_metrics() returns {"var": {..., "historical": {"95": 0.05}, "parametric": ...}, "cvar": {...}}
    assert "historical" in body["var"]["var"]
    assert "95" in body["var"]["var"]["historical"]


def test_risk_metrics_endpoint_with_equity_curve():
    payload = {
        "equity_curve": [100, 102, 99, 101, 105, 103, 108, 110, 107, 112],
    }
    client = _request()
    r = client.post("/api/platform/risk-metrics", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "drawdown" in body
    assert body["drawdown"]["max_drawdown"] <= 0


def test_risk_metrics_missing_returns_returns_422():
    client = _request()
    r = client.post("/api/platform/risk-metrics", json={})
    assert r.status_code == 422


def test_risk_metrics_empty_returns_returns_422():
    client = _request()
    r = client.post("/api/platform/risk-metrics", json={"returns": []})
    assert r.status_code == 422


def test_portfolio_optimize_max_sharpe():
    payload = {
        "returns_panel": {
            "A": [0.01, -0.005, 0.02, -0.01, 0.015, -0.02, 0.005, 0.012, -0.003, 0.008, 0.011, -0.004],
            "B": [0.008, -0.003, 0.018, -0.008, 0.013, -0.018, 0.003, 0.010, -0.001, 0.006, 0.009, -0.002],
        },
        "method": "max_sharpe",
    }
    client = _request()
    r = client.post("/api/platform/portfolio-optimize", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "max_sharpe"
    assert "weights" in body
    assert set(body["weights"].keys()) == {"A", "B"}
    assert "shrinkage_intensity" in body


def test_portfolio_optimize_min_variance():
    payload = {
        "returns_panel": {
            "A": [0.01, -0.005, 0.02, -0.01, 0.015, -0.02, 0.005, 0.012, -0.003, 0.008, 0.011, -0.004],
            "B": [0.008, -0.003, 0.018, -0.008, 0.013, -0.018, 0.003, 0.010, -0.001, 0.006, 0.009, -0.002],
        },
        "method": "min_variance",
    }
    client = _request()
    r = client.post("/api/platform/portfolio-optimize", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "min_variance"
    assert "weights" in body


def test_portfolio_optimize_hrp():
    payload = {
        "returns_panel": {
            "A": [0.01, -0.005, 0.02, -0.01, 0.015, -0.02, 0.005, 0.012, -0.003, 0.008],
            "B": [0.008, -0.003, 0.018, -0.008, 0.013, -0.018, 0.003, 0.010, -0.001, 0.006],
            "C": [0.012, -0.006, 0.024, -0.012, 0.018, -0.024, 0.006, 0.014, -0.004, 0.010],
        },
        "method": "hrp",
    }
    client = _request()
    r = client.post("/api/platform/portfolio-optimize", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "hrp"
    assert len(body["weights"]) == 3


def test_portfolio_optimize_black_litterman():
    payload = {
        "returns_panel": {
            "A": [0.01, -0.005, 0.02, -0.01, 0.015, -0.02, 0.005, 0.012, -0.003, 0.008],
            "B": [0.008, -0.003, 0.018, -0.008, 0.013, -0.018, 0.003, 0.010, -0.001, 0.006],
        },
        "method": "black_litterman",
        "market_weights": {"A": 0.5, "B": 0.5},
        "views": [
            {"assets": {"A": 1.0}, "expected_return": 0.15, "confidence": 0.9},
        ],
    }
    client = _request()
    r = client.post("/api/platform/portfolio-optimize", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "black_litterman"
    assert set(body["weights"].keys()) == {"A", "B"}


def test_portfolio_optimize_bl_without_market_weights_422():
    payload = {
        "returns_panel": {
            "A": [0.01, -0.005, 0.02, -0.01, 0.015, -0.02],
            "B": [0.008, -0.003, 0.018, -0.008, 0.013, -0.018],
        },
        "method": "black_litterman",
    }
    client = _request()
    r = client.post("/api/platform/portfolio-optimize", json=payload)
    assert r.status_code == 422


def test_portfolio_optimize_missing_panel_422():
    client = _request()
    r = client.post("/api/platform/portfolio-optimize", json={})
    assert r.status_code == 422


def test_portfolio_optimize_empty_panel_422():
    client = _request()
    r = client.post("/api/platform/portfolio-optimize", json={"returns_panel": {}})
    assert r.status_code == 422
