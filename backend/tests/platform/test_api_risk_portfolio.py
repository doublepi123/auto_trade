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


# ---------------------------------------------------------------------------
# P214 — CPCV endpoint
# ---------------------------------------------------------------------------


def test_cpcv_endpoint_enumerates_splits():
    client = _request()
    r = client.post(
        "/api/platform/cpcv",
        json={"n_samples": 10, "n_groups": 5, "k_test": 1, "purge": 0, "embargo": 0},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["splits"]) == 5
    assert body["summary"]["n_splits"] == 5
    all_test = sorted(i for s in body["splits"] for i in s["test_idx"])
    assert all_test == list(range(10))


def test_cpcv_endpoint_invalid_422():
    client = _request()
    r = client.post("/api/platform/cpcv", json={"n_samples": 5, "n_groups": 1, "k_test": 1})
    assert r.status_code == 422


def test_cpcv_endpoint_missing_fields_422():
    client = _request()
    r = client.post("/api/platform/cpcv", json={"n_samples": 10})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P215 — style analysis endpoint
# ---------------------------------------------------------------------------


def test_style_analysis_endpoint_200():
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01, 0.02, -0.01, 0.0, 0.03]
    f2 = [0.0, -0.01, 0.02, 0.01, -0.03, 0.02, 0.0, 0.01, -0.02, 0.01, 0.03, 0.0]
    r = [0.6 * f1[i] + 0.4 * f2[i] for i in range(12)]
    client = _request()
    resp = client.post(
        "/api/platform/style-analysis",
        json={
            "returns": r,
            "factor_returns": {"value": f1, "growth": f2},
            "constraint": "sum_eq_one",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "weights" in body and "r_squared" in body
    assert 0.0 <= body["r_squared"] <= 1.0
    assert abs(sum(body["weights"].values()) - 1.0) < 1e-6


def test_style_analysis_endpoint_422_empty_returns():
    client = _request()
    r = client.post(
        "/api/platform/style-analysis",
        json={"returns": [], "factor_returns": {"F1": [0.01, 0.02]}},
    )
    assert r.status_code == 422


def test_style_analysis_endpoint_422_unknown_constraint():
    client = _request()
    r = client.post(
        "/api/platform/style-analysis",
        json={"returns": [0.01, 0.02], "factor_returns": {"F1": [0.01, 0.02]}, "constraint": "banana"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P216 — turnover-aware portfolio-optimize method
# ---------------------------------------------------------------------------


def test_portfolio_optimize_turnover_method():
    panel = {
        "A": [0.01, -0.005, 0.02, 0.005, -0.01, 0.015, -0.002, 0.012, 0.003, -0.008],
        "B": [0.005, 0.002, -0.003, 0.008, 0.001, -0.002, 0.006, 0.004, -0.001, 0.007],
    }
    client = _request()
    r = client.post(
        "/api/platform/portfolio-optimize",
        json={
            "returns_panel": panel,
            "method": "turnover",
            "prev_weights": {"A": 0.5, "B": 0.5},
            "gamma": 0.5,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert abs(sum(body["weights"].values()) - 1.0) < 1e-6
    assert body["turnover"] is not None and body["turnover"] >= 0.0


def test_portfolio_optimize_turnover_missing_prev_422():
    panel = {
        "A": [0.01, 0.02, 0.0, -0.01, 0.03],
        "B": [0.005, -0.002, 0.006, 0.004, 0.007],
    }
    client = _request()
    r = client.post(
        "/api/platform/portfolio-optimize",
        json={"returns_panel": panel, "method": "turnover"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P217 — risk-budgeting portfolio-optimize method
# ---------------------------------------------------------------------------


def test_portfolio_optimize_risk_budgeting_method():
    panel = {
        "A": [0.01, -0.005, 0.02, 0.005, -0.01, 0.015, -0.002, 0.012, 0.003, -0.008,
              0.01, 0.0, -0.002, 0.005, 0.011, -0.004, 0.007, -0.001, 0.009, 0.003],
        "B": [0.005, 0.002, -0.003, 0.008, 0.001, -0.002, 0.006, 0.004, -0.001, 0.007,
              0.0, 0.003, -0.002, 0.005, 0.001, -0.003, 0.008, 0.002, -0.004, 0.006],
        "C": [0.0, 0.001, -0.002, 0.003, 0.0, -0.001, 0.002, 0.0, -0.003, 0.001,
              0.002, -0.001, 0.0, 0.003, -0.002, 0.001, 0.0, -0.002, 0.003, 0.001],
    }
    client = _request()
    r = client.post(
        "/api/platform/portfolio-optimize",
        json={
            "returns_panel": panel,
            "method": "risk_budgeting",
            "budgets": {"A": 0.5, "B": 0.3, "C": 0.2},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "risk_budgeting"
    assert abs(sum(body["weights"].values()) - 1.0) < 1e-5
    rel = body["risk_contributions"]
    assert rel is not None
    assert abs(rel["A"] - 0.5) < 1e-4
    assert abs(rel["B"] - 0.3) < 1e-4
    assert abs(rel["C"] - 0.2) < 1e-4


def test_portfolio_optimize_risk_budgeting_default_equal():
    panel = {
        "A": [0.01, -0.005, 0.02, 0.005, -0.01, 0.015, -0.002, 0.012, 0.003, -0.008],
        "B": [0.005, 0.002, -0.003, 0.008, 0.001, -0.002, 0.006, 0.004, -0.001, 0.007],
    }
    client = _request()
    r = client.post(
        "/api/platform/portfolio-optimize",
        json={"returns_panel": panel, "method": "risk_budgeting"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    rel = body["risk_contributions"]
    assert abs(rel["A"] - 0.5) < 1e-4 and abs(rel["B"] - 0.5) < 1e-4


# ---------------------------------------------------------------------------
# P213 — regime endpoint
# ---------------------------------------------------------------------------


def test_regime_endpoint_200():
    closes = [100.0 + i for i in range(101)]
    client = _request()
    r = client.post("/api/platform/regime", json={"closes": closes})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["regime"] in {"bull", "bear", "sideways"}
    assert "bull" in body and "bear" in body and "sideways" in body


def test_regime_endpoint_422_empty():
    client = _request()
    r = client.post("/api/platform/regime", json={"closes": []})
    assert r.status_code == 422


def test_regime_endpoint_422_too_short():
    client = _request()
    r = client.post("/api/platform/regime", json={"closes": [1.0] * 10})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P218 — trade excursion endpoint
# ---------------------------------------------------------------------------


def test_trade_excursion_endpoint_200():
    bars = [
        {"timestamp": "2024-01-01T09:30:00", "open": 100, "high": 101, "low": 100, "close": 100, "volume": 1000, "symbol": "X"},
        {"timestamp": "2024-01-01T09:31:00", "open": 100, "high": 105, "low": 102, "close": 104, "volume": 1000, "symbol": "X"},
        {"timestamp": "2024-01-01T09:32:00", "open": 104, "high": 99, "low": 98, "close": 99, "volume": 1000, "symbol": "X"},
        {"timestamp": "2024-01-01T09:33:00", "open": 99, "high": 103, "low": 101, "close": 102, "volume": 1000, "symbol": "X"},
        {"timestamp": "2024-01-01T09:34:00", "open": 102, "high": 108, "low": 106, "close": 107, "volume": 1000, "symbol": "X"},
    ]
    trades = [
        {"entry_time": "2024-01-01T09:31:00", "exit_time": "2024-01-01T09:34:00", "side": "BUY", "entry_price": 100.0, "exit_price": 108.0},
    ]
    client = _request()
    r = client.post("/api/platform/trade-excursion", json={"trades": trades, "bars": bars})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["num_trades"] == 1
    assert body["trades"][0]["mfe"] == 8.0


def test_trade_excursion_endpoint_422_missing_trades():
    client = _request()
    r = client.post("/api/platform/trade-excursion", json={"bars": []})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P219 — implementation shortfall endpoint
# ---------------------------------------------------------------------------


def test_shortfall_endpoint_200():
    client = _request()
    r = client.post(
        "/api/platform/shortfall",
        json={
            "order": {
                "symbol": "A.US", "side": "BUY", "ordered_quantity": 100,
                "arrival_price": 100,
            },
            "fills": [{"quantity": 100, "price": 101, "commission": 1}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["realized_cost"] == 100.0
    assert body["fees"] == 1.0
    assert body["total_shortfall"] == 101.0


def test_shortfall_endpoint_422_missing_order():
    client = _request()
    r = client.post("/api/platform/shortfall", json={"fills": []})
    assert r.status_code == 422


def test_shortfall_endpoint_422_close_without_price():
    client = _request()
    r = client.post(
        "/api/platform/shortfall",
        json={
            "order": {"symbol": "A.US", "side": "BUY", "ordered_quantity": 100,
                      "arrival_price": 100, "benchmark": "close"},
            "fills": [{"quantity": 100, "price": 100}],
        },
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P220 — returns calendar endpoint
# ---------------------------------------------------------------------------


def test_returns_calendar_endpoint_200():
    client = _request()
    r = client.post(
        "/api/platform/returns-calendar",
        json={"returns": [0.01, -0.02, 0.03, 0.015, -0.01, 0.02]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "monthly" in body and "yearly" in body and "weekday" in body
    assert "streaks" in body and "summary" in body


def test_returns_calendar_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/returns-calendar", json={})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P221 — stress report endpoint
# ---------------------------------------------------------------------------


def test_stress_report_endpoint_200():
    client = _request()
    r = client.post(
        "/api/platform/stress-report",
        json={"positions": {"A.US": [100, 100], "B.US": [200, 50]}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "scenarios" in body and body["worst_scenario"] is not None
    assert body["worst_pnl"] < 0


def test_stress_report_endpoint_422_missing_positions():
    client = _request()
    r = client.post("/api/platform/stress-report", json={})
    assert r.status_code == 422


def test_stress_report_endpoint_422_empty_positions():
    client = _request()
    r = client.post("/api/platform/stress-report", json={"positions": {}})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P222 — walk-forward stability endpoint
# ---------------------------------------------------------------------------


def test_stability_endpoint_200():
    client = _request()
    r = client.post(
        "/api/platform/stability",
        json={
            "wf_results": [
                {"params": {"a": 1}, "in_sample_sharpe": 1.5, "out_of_sample_sharpe": 1.4},
                {"params": {"a": 2}, "in_sample_sharpe": 1.3, "out_of_sample_sharpe": 1.2},
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "degradation" in body and "neighborhood_stability" in body and "drift" in body


def test_stability_endpoint_422_empty():
    client = _request()
    r = client.post("/api/platform/stability", json={"wf_results": []})
    assert r.status_code == 422


def test_stability_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/stability", json={})
    assert r.status_code == 422

