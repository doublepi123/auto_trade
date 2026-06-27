"""Tests for P211 / P212 risk-metrics and portfolio-optimize endpoints."""

from __future__ import annotations

import json
import math
import os

import pytest

# Set test DB and credential key BEFORE app imports.
os.environ.setdefault("AUTO_TRADE_DATABASE_URL", "sqlite:///./data/test_p211_p212.db")
os.environ.setdefault("AUTO_TRADE_ENV", "test")
os.environ.setdefault("AUTO_TRADE_CREDENTIAL_KEY_PATH", "./data/test_cred_p211.pem")
os.environ.setdefault("AUTO_TRADE_API_KEY", "test-key")

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



# ---------------------------------------------------------------------------
# P223 — cointegration endpoint
# ---------------------------------------------------------------------------


def test_cointegration_endpoint_200():
    client = _request()
    n = 200
    import math as _math
    x = [100.0 + 0.3 * i for i in range(n)]
    spread_truth = [0.5 * _math.sin(2 * _math.pi * 5 * i / n) for i in range(n)]
    y = [2.0 * xi + 1.0 + s for xi, s in zip(x, spread_truth)]
    r = client.post("/api/platform/cointegration", json={"y": y, "x": x})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "beta" in body and "alpha" in body and "spread" in body
    assert "current_zscore" in body and "half_life" in body and "durbin_watson" in body


def test_cointegration_endpoint_422_mismatch():
    client = _request()
    r = client.post("/api/platform/cointegration", json={"y": [1.0, 2.0], "x": [1.0]})
    assert r.status_code == 422


def test_cointegration_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/cointegration", json={"y": [1.0, 2.0]})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P224 — Kelly endpoint
# ---------------------------------------------------------------------------


def test_kelly_endpoint_binary_200():
    client = _request()
    r = client.post("/api/platform/kelly", json={"win_prob": 0.55, "win_size": 1.0, "loss_size": 1.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert abs(body["full_kelly"] - 0.1) < 1e-9
    assert "half_kelly" in body and "has_edge" in body


def test_kelly_endpoint_continuous_200():
    client = _request()
    rs = [0.01, -0.02, 0.03, 0.0, 0.02, -0.01, 0.04, 0.01]
    r = client.post("/api/platform/kelly", json={"returns": rs})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "continuous"


def test_kelly_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/kelly", json={})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P225 — volatility endpoint
# ---------------------------------------------------------------------------


def test_volatility_endpoint_200():
    client = _request()
    rs = [0.01, -0.02, 0.03, -0.01, 0.02, 0.0, -0.01, 0.015]
    r = client.post("/api/platform/volatility", json={"returns": rs})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "ewma" in body and "garch" in body and body["parkinson"] is None


def test_volatility_endpoint_with_highs_lows():
    client = _request()
    rs = [0.01, -0.02, 0.03, -0.01, 0.02]
    r = client.post(
        "/api/platform/volatility",
        json={"returns": rs, "highs": [101, 99, 103, 100, 102], "lows": [99, 97, 100, 98, 100]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parkinson"] is not None


def test_volatility_endpoint_422_short():
    client = _request()
    r = client.post("/api/platform/volatility", json={"returns": [0.01]})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P226 — microstructure endpoint
# ---------------------------------------------------------------------------


def test_microstructure_endpoint_200():
    client = _request()
    vols = [100.0] * 6
    opens = [10.0] * 6
    closes = [11.0, 10.5, 9.5, 9.0, 11.0, 10.5]
    r = client.post(
        "/api/platform/microstructure",
        json={"volumes": vols, "opens": opens, "closes": closes, "bucket_size": 100.0},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "vpin" in body and "ofi" in body and "kyle_lambda" in body


def test_microstructure_endpoint_422_mismatch():
    client = _request()
    r = client.post(
        "/api/platform/microstructure",
        json={"volumes": [100.0], "opens": [10.0], "closes": [11.0, 12.0]},
    )
    assert r.status_code == 422


def test_microstructure_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/microstructure", json={"volumes": [100.0]})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P227 — Almgren-Chriss endpoint
# ---------------------------------------------------------------------------


def test_execution_cost_endpoint_200():
    client = _request()
    r = client.post("/api/platform/execution-cost",
                    json={"total_shares": 1000.0, "n_slices": 5, "risk_aversion": 0.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "trajectory" in body and "expected_cost" in body and "risk" in body


def test_execution_cost_endpoint_frontier():
    client = _request()
    r = client.post("/api/platform/execution-cost",
                    json={"total_shares": 1000.0, "n_slices": 10, "frontier": True})
    assert r.status_code == 200, r.text
    assert "frontier" in r.json()


def test_execution_cost_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/execution-cost", json={"total_shares": 100.0})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P228 — Hawkes endpoint
# ---------------------------------------------------------------------------


def test_hawkes_endpoint_200():
    client = _request()
    r = client.post("/api/platform/hawkes", json={"events": [1.0, 1.5, 2.0, 2.2, 3.0, 4.0]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "branching_ratio" in body and "log_likelihood" in body


def test_hawkes_endpoint_422_empty():
    client = _request()
    r = client.post("/api/platform/hawkes", json={"events": []})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P229 — historical stress endpoint
# ---------------------------------------------------------------------------


def test_historical_stress_endpoint_200():
    client = _request()
    r = client.post("/api/platform/historical-stress",
                    json={"positions": {"A.US": [100, 100.0], "B.US": [200, 50.0]}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "worst_episode" in body and "per_episode" in body


def test_historical_stress_endpoint_custom_episodes():
    client = _request()
    r = client.post("/api/platform/historical-stress",
                    json={"positions": {"A.US": [100, 100.0]},
                          "episodes": [{"name": "crash", "returns": {"A.US": -0.5}}]})
    assert r.status_code == 200, r.text
    assert r.json()["worst_episode"] == "crash"


def test_historical_stress_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/historical-stress", json={})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P230 — factor risk endpoint
# ---------------------------------------------------------------------------


def test_factor_risk_endpoint_200():
    client = _request()
    r = client.post("/api/platform/factor-risk",
                    json={
                        "weights": {"A": 0.5, "B": 0.5},
                        "exposures": {"A": {"MKT": 1.2}, "B": {"MKT": 0.8}},
                        "factor_cov": {"MKT": {"MKT": 0.04}},
                        "idio_var": {"A": 0.01, "B": 0.01},
                    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "factor_variance" in body and "idiosyncratic_variance" in body


def test_factor_risk_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/factor-risk", json={})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P231 — sensitivity endpoint
# ---------------------------------------------------------------------------


def test_sensitivity_endpoint_200():
    client = _request()
    records = []
    for a in [1, 2, 3, 4]:
        for b in [1, 2, 3]:
            records.append({"params": {"a": a, "b": b}, "metric": float(a * 10 + b)})
    r = client.post("/api/platform/sensitivity", json={"records": records})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "importance_ranking" in body and "first_order" in body


def test_sensitivity_endpoint_422_empty():
    client = _request()
    r = client.post("/api/platform/sensitivity", json={"records": []})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P232 — EVT endpoint
# ---------------------------------------------------------------------------


def test_evt_endpoint_200():
    client = _request()
    losses = [float(abs(i - 25)) * 0.1 for i in range(50)]
    r = client.post("/api/platform/evt",
                    json={"losses": losses, "threshold": 1.5, "confidence_levels": [0.95, 0.99]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "gpd" in body and "var" in body and "cvar" in body


def test_evt_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/evt", json={"losses": [0.1, 0.2]})
    assert r.status_code == 422


def test_evt_endpoint_422_empty():
    client = _request()
    r = client.post("/api/platform/evt", json={"losses": [], "threshold": 0.5})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P243 — options pricing + Greeks endpoint
# ---------------------------------------------------------------------------


def test_options_pricing_endpoint_call_200():
    client = _request()
    r = client.post("/api/platform/options-pricing", json={
        "option_type": "call", "spot": 100.0, "strike": 100.0,
        "time_to_expiry": 1.0, "risk_free": 0.05, "volatility": 0.2,
        "dividend_yield": 0.0,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert abs(body["price"] - 10.450583572185565) < 1e-9
    assert "delta" in body and "gamma" in body and "vanna" in body and "volga" in body


def test_options_pricing_endpoint_put_200():
    client = _request()
    r = client.post("/api/platform/options-pricing", json={
        "option_type": "put", "spot": 100.0, "strike": 100.0,
        "time_to_expiry": 1.0, "risk_free": 0.05, "volatility": 0.2,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert abs(body["price"] - 5.573526023256497) < 1e-9


def test_options_pricing_endpoint_422_bad_type():
    client = _request()
    r = client.post("/api/platform/options-pricing", json={
        "option_type": "straddle", "spot": 100.0, "strike": 100.0,
        "time_to_expiry": 1.0, "risk_free": 0.05, "volatility": 0.2,
    })
    assert r.status_code == 422


def test_options_pricing_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/options-pricing", json={"option_type": "call", "spot": 100.0})
    assert r.status_code == 422


def test_options_pricing_endpoint_422_nonpositive_vol():
    client = _request()
    r = client.post("/api/platform/options-pricing", json={
        "option_type": "call", "spot": 100.0, "strike": 100.0,
        "time_to_expiry": 1.0, "risk_free": 0.05, "volatility": 0.0,
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P244 — implied volatility + SVI endpoint
# ---------------------------------------------------------------------------


def test_implied_volatility_endpoint_iv_mode_200():
    client = _request()
    # First price a call, then invert.
    r = client.post("/api/platform/options-pricing", json={
        "option_type": "call", "spot": 100.0, "strike": 100.0,
        "time_to_expiry": 1.0, "risk_free": 0.05, "volatility": 0.2,
    })
    price = r.json()["price"]
    r2 = client.post("/api/platform/implied-volatility", json={
        "mode": "iv", "option_type": "call", "price": price,
        "spot": 100.0, "strike": 100.0, "time_to_expiry": 1.0, "risk_free": 0.05,
    })
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert abs(body["implied_vol"] - 0.2) < 1e-7


def test_implied_volatility_endpoint_svi_mode_200():
    client = _request()
    ks = [k * 0.1 for k in range(-20, 21)]
    ivs = [0.2 + 0.01 * abs(k) for k in ks]
    r = client.post("/api/platform/implied-volatility", json={
        "mode": "svi", "log_moneyness": ks, "implied_vols": ivs, "time_to_expiry": 1.0,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "svi"
    assert "a" in body and "b" in body and "rho" in body and "m" in body and "sigma" in body
    assert body["sigma"] > 0.0
    assert -1.0 <= body["rho"] <= 1.0


def test_implied_volatility_endpoint_422_bad_type():
    client = _request()
    r = client.post("/api/platform/implied-volatility", json={
        "mode": "iv", "option_type": "straddle", "price": 5.0,
        "spot": 100.0, "strike": 100.0, "time_to_expiry": 1.0, "risk_free": 0.05,
    })
    assert r.status_code == 422


def test_implied_volatility_endpoint_422_missing_price():
    client = _request()
    r = client.post("/api/platform/implied-volatility", json={
        "mode": "iv", "option_type": "call",
        "spot": 100.0, "strike": 100.0, "time_to_expiry": 1.0, "risk_free": 0.05,
    })
    assert r.status_code == 422


def test_implied_volatility_endpoint_422_svi_few_points():
    client = _request()
    r = client.post("/api/platform/implied-volatility", json={
        "mode": "svi", "log_moneyness": [0.0, 0.1, 0.2],
        "implied_vols": [0.2, 0.21, 0.22], "time_to_expiry": 1.0,
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P245 — Kalman filter endpoint
# ---------------------------------------------------------------------------


def test_kalman_filter_endpoint_200():
    client = _request()
    obs = [[5.0 + 0.1 * (i % 3 - 1)] for i in range(20)]
    r = client.post("/api/platform/kalman-filter", json={
        "observations": obs, "F": [[1.0]], "H": [[1.0]], "Q": [[0.0]], "R": [[1.0]],
        "x0": [0.0], "P0": [[100.0]], "smooth": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "filtered_means" in body and "smoothed_means" in body
    assert abs(body["filtered_means"][-1][0] - 5.0) < 0.5


def test_kalman_filter_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/kalman-filter", json={"observations": [[1.0]]})
    assert r.status_code == 422


def test_kalman_filter_endpoint_422_empty():
    client = _request()
    r = client.post("/api/platform/kalman-filter", json={
        "observations": [], "F": [[1.0]], "H": [[1.0]], "Q": [[0.0]], "R": [[1.0]],
        "x0": [0.0], "P0": [[1.0]],
    })
    assert r.status_code == 422


def test_kalman_filter_endpoint_422_singular():
    client = _request()
    r = client.post("/api/platform/kalman-filter", json={
        "observations": [[1.0]], "F": [[1.0]], "H": [[1.0]], "Q": [[0.0]], "R": [[0.0]],
        "x0": [0.0], "P0": [[0.0]],
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P246 — stochastic processes endpoint
# ---------------------------------------------------------------------------


def test_stochastic_processes_gbm_200():
    client = _request()
    r = client.post("/api/platform/stochastic-processes", json={
        "process": "gbm", "s0": 100.0, "mu": 0.05, "sigma": 0.2,
        "horizon": 1.0, "n_steps": 50, "seed": 1,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["process"] == "gbm"
    assert len(body["path"]) == 51
    assert body["path"][0] == 100.0
    assert "moments" in body


def test_stochastic_processes_cir_200_positive():
    client = _request()
    r = client.post("/api/platform/stochastic-processes", json={
        "process": "cir", "r0": 0.05, "kappa": 2.0, "theta": 0.05, "sigma": 0.1,
        "horizon": 5.0, "n_steps": 500, "seed": 11,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert all(v >= 0.0 for v in body["path"])


def test_stochastic_processes_422_bad_process():
    client = _request()
    r = client.post("/api/platform/stochastic-processes", json={
        "process": "heston", "horizon": 1.0, "n_steps": 10,
    })
    assert r.status_code == 422


def test_stochastic_processes_422_missing_param():
    client = _request()
    r = client.post("/api/platform/stochastic-processes", json={
        "process": "gbm", "s0": 100.0, "horizon": 1.0, "n_steps": 10,
    })
    assert r.status_code == 422


def test_stochastic_processes_422_invalid_sigma():
    client = _request()
    r = client.post("/api/platform/stochastic-processes", json={
        "process": "gbm", "s0": 100.0, "mu": 0.05, "sigma": 0.0,
        "horizon": 1.0, "n_steps": 10,
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P247 — stat-arb signals endpoint
# ---------------------------------------------------------------------------


def test_stat_arb_signals_endpoint_200():
    client = _request()
    base = [100.0 + i for i in range(60)]
    y = [b + 10.0 * (i % 7 - 3) * 0.3 for i, b in enumerate(base)]
    x = base
    r = client.post("/api/platform/stat-arb-signals", json={
        "y": y, "x": x, "entry": 1.0, "exit": 0.3,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "spread" in body and "signals" in body and "half_life" in body
    assert len(body["signals"]) == 60


def test_stat_arb_signals_endpoint_422_mismatch():
    client = _request()
    r = client.post("/api/platform/stat-arb-signals", json={"y": [1, 2], "x": [1]})
    assert r.status_code == 422


def test_stat_arb_signals_endpoint_422_empty():
    client = _request()
    r = client.post("/api/platform/stat-arb-signals", json={"y": [], "x": []})
    assert r.status_code == 422


def test_stat_arb_signals_endpoint_422_bad_thresholds():
    client = _request()
    r = client.post("/api/platform/stat-arb-signals", json={
        "y": [100.0, 101.0], "x": [100.0, 100.0], "entry": 0.3, "exit": 0.5,
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P248 — robust statistics endpoint
# ---------------------------------------------------------------------------


def test_robust_statistics_endpoint_200():
    client = _request()
    r = client.post("/api/platform/robust-statistics", json={
        "xs": [1.0, 2.0, 3.0, 4.0, 5.0],
        "y": [2.0 + 3.0 * v for v in [1.0, 2.0, 3.0, 4.0, 5.0]],
        "x": [1.0, 2.0, 3.0, 4.0, 5.0],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert abs(body["median"] - 3.0) < 1e-9
    assert body["theil_sen_slope"] == 3.0
    assert "huber_location" in body


def test_robust_statistics_endpoint_422_empty():
    client = _request()
    r = client.post("/api/platform/robust-statistics", json={"xs": []})
    assert r.status_code == 422


def test_robust_statistics_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/robust-statistics", json={})
    assert r.status_code == 422


def test_robust_statistics_endpoint_422_partial_regression():
    client = _request()
    r = client.post("/api/platform/robust-statistics", json={"xs": [1.0, 2.0], "y": [1.0, 2.0]})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P249 — bandits endpoint
# ---------------------------------------------------------------------------


def test_bandits_endpoint_ucb1_200():
    client = _request()
    r = client.post("/api/platform/bandits", json={
        "algorithm": "ucb1", "true_means": [0.1, 0.9, 0.2], "n_steps": 500, "seed": 42,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["algorithm"] == "ucb1"
    assert body["arm_counts"][1] > body["arm_counts"][0]
    assert "cumulative_regret" in body


def test_bandits_endpoint_thompson_gaussian_200():
    client = _request()
    r = client.post("/api/platform/bandits", json={
        "algorithm": "thompson_gaussian", "true_means": [0.1, 0.9],
        "sigmas": [0.1, 0.1], "n_steps": 200, "seed": 3,
    })
    assert r.status_code == 200, r.text


def test_bandits_endpoint_422_bad_algorithm():
    client = _request()
    r = client.post("/api/platform/bandits", json={
        "algorithm": "softmax", "true_means": [0.5], "n_steps": 10,
    })
    assert r.status_code == 422


def test_bandits_endpoint_422_missing_means():
    client = _request()
    r = client.post("/api/platform/bandits", json={"algorithm": "ucb1", "n_steps": 10})
    assert r.status_code == 422


def test_bandits_endpoint_422_thompson_gaussian_no_sigmas():
    client = _request()
    r = client.post("/api/platform/bandits", json={
        "algorithm": "thompson_gaussian", "true_means": [0.1, 0.9], "n_steps": 10,
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P250 — LOESS endpoint
# ---------------------------------------------------------------------------


def test_loess_endpoint_200_linear():
    client = _request()
    x = [float(i) for i in range(10)]
    y = [2.0 + 3.0 * xi for xi in x]
    r = client.post("/api/platform/loess", json={"x": x, "y": y, "bandwidth": 0.5, "iterations": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    for xv, yv, sv in zip(x, y, body["smoothed"]):
        assert abs(sv - yv) < 1e-6


def test_loess_endpoint_422_mismatch():
    client = _request()
    r = client.post("/api/platform/loess", json={"x": [1, 2, 3], "y": [1, 2]})
    assert r.status_code == 422


def test_loess_endpoint_422_empty():
    client = _request()
    r = client.post("/api/platform/loess", json={"x": [], "y": []})
    assert r.status_code == 422


def test_loess_endpoint_422_bad_bandwidth():
    client = _request()
    r = client.post("/api/platform/loess", json={"x": [1, 2, 3], "y": [1, 2, 3], "bandwidth": 0.0})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P251 — smart order routing endpoint
# ---------------------------------------------------------------------------


def test_smart_order_routing_endpoint_buy_200():
    client = _request()
    r = client.post("/api/platform/smart-order-routing", json={
        "side": "buy", "quantity": 100,
        "venues": [
            {"venue": "A", "bid": 99.8, "bid_size": 200, "ask": 100.1, "ask_size": 100},
            {"venue": "C", "bid": 99.7, "bid_size": 500, "ask": 100.05, "ask_size": 200},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["child_orders"][0]["venue"] == "C"
    assert body["filled_quantity"] == 100


def test_smart_order_routing_endpoint_422_bad_side():
    client = _request()
    r = client.post("/api/platform/smart-order-routing", json={
        "side": "hold", "quantity": 100, "venues": [{"venue": "A", "bid": 99, "bid_size": 10, "ask": 100, "ask_size": 10}],
    })
    assert r.status_code == 422


def test_smart_order_routing_endpoint_422_empty_venues():
    client = _request()
    r = client.post("/api/platform/smart-order-routing", json={"side": "buy", "quantity": 100, "venues": []})
    assert r.status_code == 422


def test_smart_order_routing_endpoint_422_missing_qty():
    client = _request()
    r = client.post("/api/platform/smart-order-routing", json={
        "side": "buy", "venues": [{"venue": "A", "bid": 99, "bid_size": 10, "ask": 100, "ask_size": 10}],
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P251 — smart order routing endpoint
# ---------------------------------------------------------------------------


def test_smart_order_routing_endpoint_buy_200():
    client = _request()
    r = client.post("/api/platform/smart-order-routing", json={
        "side": "buy", "quantity": 100,
        "venues": [
            {"venue": "A", "bid": 99.8, "bid_size": 200, "ask": 100.1, "ask_size": 100},
            {"venue": "C", "bid": 99.7, "bid_size": 500, "ask": 100.05, "ask_size": 200},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["child_orders"][0]["venue"] == "C"
    assert body["filled_quantity"] == 100


def test_smart_order_routing_endpoint_422_bad_side():
    client = _request()
    r = client.post("/api/platform/smart-order-routing", json={
        "side": "hold", "quantity": 100,
        "venues": [{"venue": "A", "bid": 99, "bid_size": 10, "ask": 100, "ask_size": 10}],
    })
    assert r.status_code == 422


def test_smart_order_routing_endpoint_422_empty_venues():
    client = _request()
    r = client.post("/api/platform/smart-order-routing", json={"side": "buy", "quantity": 100, "venues": []})
    assert r.status_code == 422


def test_smart_order_routing_endpoint_422_missing_qty():
    client = _request()
    r = client.post("/api/platform/smart-order-routing", json={
        "side": "buy",
        "venues": [{"venue": "A", "bid": 99, "bid_size": 10, "ask": 100, "ask_size": 10}],
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P252 — vine copula endpoint
# ---------------------------------------------------------------------------


def test_vine_copula_endpoint_200():
    client = _request()
    import math, random
    rng = random.Random(1)
    data = [[], [], []]
    for _ in range(150):
        z0 = rng.gauss(0.0, 1.0)
        z1 = rng.gauss(0.0, 1.0)
        z2 = rng.gauss(0.0, 1.0)
        data[0].append(z0)
        data[1].append(0.6 * z0 + 0.8 * z1)
        data[2].append(0.6 * z0 + 0.8 * z2)
    r = client.post("/api/platform/vine-copula", json={
        "data": data, "structure": "c-vine", "family": "gaussian",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["n_assets"] == 3
    assert "aic" in body and "bic" in body


def test_vine_copula_endpoint_422_empty():
    client = _request()
    r = client.post("/api/platform/vine-copula", json={"data": []})
    assert r.status_code == 422


def test_vine_copula_endpoint_422_bad_structure():
    client = _request()
    r = client.post("/api/platform/vine-copula", json={
        "data": [[1.0, 2.0, 3.0], [2.0, 3.0, 1.0], [3.0, 1.0, 2.0]],
        "structure": "r-vine",
    })
    assert r.status_code == 422


def test_vine_copula_endpoint_422_ragged():
    client = _request()
    r = client.post("/api/platform/vine-copula", json={
        "data": [[1.0, 2.0, 3.0], [1.0, 2.0]],
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P253 — American options endpoint
# ---------------------------------------------------------------------------


def test_american_options_endpoint_put_200():
    client = _request()
    r = client.post("/api/platform/american-options", json={
        "option_type": "put", "spot": 100.0, "strike": 100.0,
        "time_to_expiry": 1.0, "risk_free": 0.05, "volatility": 0.2,
        "steps": 200, "exercise": "american",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["exercise"] == "american"
    assert body["price"] > 0.0
    assert body["risk_neutral_prob"] > 0.0


def test_american_options_endpoint_european_converges():
    client = _request()
    r = client.post("/api/platform/american-options", json={
        "option_type": "call", "spot": 100.0, "strike": 100.0,
        "time_to_expiry": 1.0, "risk_free": 0.05, "volatility": 0.2,
        "steps": 500, "exercise": "european",
    })
    assert r.status_code == 200, r.text
    # European binomial ≈ 10.45 (BS)
    assert abs(r.json()["price"] - 10.450583572185565) < 0.05


def test_american_options_endpoint_422_bad_type():
    client = _request()
    r = client.post("/api/platform/american-options", json={
        "option_type": "straddle", "spot": 100.0, "strike": 100.0,
        "time_to_expiry": 1.0, "risk_free": 0.05, "volatility": 0.2,
    })
    assert r.status_code == 422


def test_american_options_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/american-options", json={"option_type": "call", "spot": 100.0})
    assert r.status_code == 422


def test_american_options_endpoint_422_bad_exercise():
    client = _request()
    r = client.post("/api/platform/american-options", json={
        "option_type": "call", "spot": 100.0, "strike": 100.0,
        "time_to_expiry": 1.0, "risk_free": 0.05, "volatility": 0.2,
        "exercise": "bermudan",
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P254 — Heston endpoint
# ---------------------------------------------------------------------------


def test_heston_endpoint_200():
    client = _request()
    r = client.post("/api/platform/heston", json={
        "option_type": "call", "spot": 100.0, "strike": 100.0,
        "time_to_expiry": 1.0, "risk_free": 0.05,
        "v0": 0.04, "kappa": 2.0, "theta": 0.04, "sigma": 0.3, "rho": -0.5,
        "n_paths": 5000, "n_steps": 32, "seed": 1,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["price"] > 0.0
    assert body["standard_error"] > 0.0
    assert body["n_paths"] == 5000


def test_heston_endpoint_422_bad_type():
    client = _request()
    r = client.post("/api/platform/heston", json={
        "option_type": "straddle", "spot": 100.0, "strike": 100.0,
        "time_to_expiry": 1.0, "risk_free": 0.05,
        "v0": 0.04, "kappa": 2.0, "theta": 0.04, "sigma": 0.3, "rho": -0.5,
    })
    assert r.status_code == 422


def test_heston_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/heston", json={"option_type": "call", "spot": 100.0})
    assert r.status_code == 422


def test_heston_endpoint_422_bad_rho():
    client = _request()
    r = client.post("/api/platform/heston", json={
        "option_type": "call", "spot": 100.0, "strike": 100.0,
        "time_to_expiry": 1.0, "risk_free": 0.05,
        "v0": 0.04, "kappa": 2.0, "theta": 0.04, "sigma": 0.3, "rho": 2.0,
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P255 — Nelson-Siegel-Svensson endpoint
# ---------------------------------------------------------------------------


def test_yield_curve_endpoint_200():
    client = _request()
    r = client.post("/api/platform/yield-curve", json={
        "maturities": [0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0],
        "yields": [0.02, 0.025, 0.03, 0.033, 0.038, 0.042, 0.045, 0.046],
        "evaluate_maturities": [1.0, 5.0, 10.0],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "beta0" in body and "tau1" in body and "rms" in body
    assert len(body["curve"]) == 3
    assert all(0.0 < c["zero_rate"] < 0.1 for c in body["curve"])


def test_yield_curve_endpoint_422_mismatch():
    client = _request()
    r = client.post("/api/platform/yield-curve", json={"maturities": [1.0, 2.0], "yields": [0.03]})
    assert r.status_code == 422


def test_yield_curve_endpoint_422_too_few():
    client = _request()
    r = client.post("/api/platform/yield-curve", json={"maturities": [1.0], "yields": [0.03]})
    assert r.status_code == 422


def test_yield_curve_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/yield-curve", json={"yields": [0.03, 0.04]})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P256 — fixed-income analytics endpoint
# ---------------------------------------------------------------------------


def test_fixed_income_endpoint_200():
    client = _request()
    r = client.post("/api/platform/fixed-income", json={
        "price": 95.0, "face": 100.0, "coupon": 4.0, "periods": 10,
        "spot_short": 0.03, "spot_long": 0.04, "short_maturity": 1.0, "long_maturity": 2.0,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ytm"] > 0.04
    assert body["macaulay_duration"] > 0.0
    assert "forward_rate" in body


def test_fixed_income_endpoint_422_missing():
    client = _request()
    r = client.post("/api/platform/fixed-income", json={"price": 95.0, "face": 100.0})
    assert r.status_code == 422


def test_fixed_income_endpoint_422_bad_price():
    client = _request()
    r = client.post("/api/platform/fixed-income", json={
        "price": -1.0, "face": 100.0, "coupon": 4.0, "periods": 10,
    })
    assert r.status_code == 422


def test_fixed_income_endpoint_422_periods():
    client = _request()
    r = client.post("/api/platform/fixed-income", json={
        "price": 95.0, "face": 100.0, "coupon": 4.0, "periods": 0,
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P257 — PCA endpoint
# ---------------------------------------------------------------------------


def test_pca_endpoint_200():
    client = _request()
    data = [[float(i), float(i * 2), float(i + j)] for i in range(10) for j in range(3)]
    r = client.post("/api/platform/pca", json={"data": data[:10], "n_components": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["eigenvalues"]) == 2
    assert len(body["projection"][0]) == 2
    assert abs(sum(body["explained_variance_ratio"]) - body["cumulative_variance_ratio"][-1]) < 1e-9


def test_pca_endpoint_422_ragged():
    client = _request()
    r = client.post("/api/platform/pca", json={"data": [[1.0, 2.0], [3.0]]})
    assert r.status_code == 422


def test_pca_endpoint_422_empty():
    client = _request()
    r = client.post("/api/platform/pca", json={"data": []})
    assert r.status_code == 422


def test_pca_endpoint_422_too_few_samples():
    client = _request()
    r = client.post("/api/platform/pca", json={"data": [[1.0, 2.0]]})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P259 — Spectral analysis (naive DFT periodogram) endpoint
# ---------------------------------------------------------------------------


def test_spectral_analysis_endpoint_200():
    client = _request()
    # [0,1,0,-1]*4 ⇒ 4-sample cycle; at sample_rate=16 the dominant bin sits
    # near 4 Hz.
    series = [0.0, 1.0, 0.0, -1.0] * 4
    r = client.post(
        "/api/platform/spectral-analysis",
        json={"series": series, "sample_rate": 16.0, "bands": [[0.0, 2.0], [2.0, 8.0]]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("dominant_frequency", "dominant_bin", "spectral_entropy",
                "frequencies", "periodogram", "band_labels", "band_energy", "n", "sample_rate"):
        assert key in body, f"missing field {key!r}"
    assert 3.0 <= body["dominant_frequency"] <= 5.0
    assert 0.0 <= body["spectral_entropy"] <= 1.0
    assert len(body["band_energy"]) == 2


def test_spectral_analysis_endpoint_422_empty_series():
    client = _request()
    r = client.post("/api/platform/spectral-analysis", json={"series": []})
    assert r.status_code == 422


def test_spectral_analysis_endpoint_422_bad_sample_rate():
    client = _request()
    r = client.post(
        "/api/platform/spectral-analysis",
        json={"series": [1.0, 2.0, 3.0], "sample_rate": 0.0},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P260 — Cycle detection (autocorrelation / Ljung-Box / seasonal strength)
# ---------------------------------------------------------------------------


def test_cycle_detection_endpoint_200():
    client = _request()
    # Clean period-5 sinusoid across 10 cycles ⇒ a strong candidate at lag 5.
    series = [
        math.sin(2.0 * math.pi * i / 5.0)
        for i in range(50)
    ]
    r = client.post(
        "/api/platform/cycle-detection",
        json={"series": series, "min_period": 2, "max_period": 12},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("candidates", "seasonal_strength", "ljung_box_stat", "n", "max_period"):
        assert key in body, f"missing field {key!r}"
    assert isinstance(body["candidates"], list)
    assert body["candidates"], "expected at least one candidate"
    for cand in body["candidates"]:
        assert set(cand.keys()) == {"period", "autocorrelation", "score"}
    assert 0.0 <= body["seasonal_strength"] <= 1.0
    assert body["ljung_box_stat"] >= 0.0
    # Top candidate should align with the true period 5.
    assert body["candidates"][0]["period"] == 5


def test_cycle_detection_endpoint_422_empty_series():
    client = _request()
    r = client.post("/api/platform/cycle-detection", json={"series": []})
    assert r.status_code == 422


def test_cycle_detection_endpoint_422_invalid_period():
    client = _request()
    r = client.post(
        "/api/platform/cycle-detection",
        json={"series": [1.0] * 20, "min_period": 0, "max_period": 5},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P261 — change-point detection endpoint
# ---------------------------------------------------------------------------


def test_change_point_endpoint_200_step_series():
    client = _request()
    series = [1.0] * 20 + [5.0] * 20
    r = client.post("/api/platform/change-point", json={"series": series})
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("change_points", "best_index", "confidence", "mean_score", "variance_score", "segments"):
        assert key in body, f"missing field {key!r}"
    assert body["best_index"] is not None
    assert abs(body["best_index"] - 20) <= 2
    assert 0.0 <= body["confidence"] <= 1.0
    assert body["change_points"], "expected at least one change point for a step series"
    for cp in body["change_points"]:
        assert set(cp.keys()) == {"index", "mean_shift_score", "variance_shift_score", "score"}
    assert body["segments"][0]["start"] == 0
    assert body["segments"][-1]["end"] == len(series)


def test_change_point_endpoint_200_with_params():
    client = _request()
    series = [1.0] * 15 + [5.0] * 15 + [9.0] * 15
    r = client.post(
        "/api/platform/change-point",
        json={"series": series, "min_size": 5, "max_points": 3, "threshold": 0.0},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["change_points"]) <= 3


def test_change_point_endpoint_422_empty_series():
    client = _request()
    r = client.post("/api/platform/change-point", json={"series": []})
    assert r.status_code == 422


def test_change_point_endpoint_422_missing_series():
    client = _request()
    r = client.post("/api/platform/change-point", json={})
    assert r.status_code == 422


def test_change_point_endpoint_422_too_short():
    client = _request()
    r = client.post("/api/platform/change-point", json={"series": [1.0, 2.0]})
    assert r.status_code == 422


def test_change_point_endpoint_422_invalid_min_size():
    client = _request()
    r = client.post("/api/platform/change-point", json={"series": [1.0] * 20, "min_size": 1})
    assert r.status_code == 422


def test_change_point_endpoint_422_invalid_max_points():
    client = _request()
    r = client.post("/api/platform/change-point", json={"series": [1.0] * 20, "max_points": 0})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P262 — entropy & complexity endpoint
# ---------------------------------------------------------------------------


def test_entropy_complexity_endpoint_200():
    series = [float(i) + 0.1 * ((i * 7) % 11) for i in range(150)]
    client = _request()
    r = client.post("/api/platform/entropy-complexity", json={"series": series})
    assert r.status_code == 200, r.text
    body = r.json()
    for key in (
        "shannon_entropy",
        "sample_entropy",
        "permutation_entropy",
        "hurst_exponent",
        "approximation",
        "n",
    ):
        assert key in body, f"missing {key!r}"
    assert body["n"] == len(series)


def test_entropy_complexity_endpoint_422_empty_series():
    client = _request()
    r = client.post("/api/platform/entropy-complexity", json={"series": []})
    assert r.status_code == 422


def test_entropy_complexity_endpoint_422_missing_series():
    client = _request()
    r = client.post("/api/platform/entropy-complexity", json={})
    assert r.status_code == 422


def test_entropy_complexity_endpoint_422_invalid_bins():
    client = _request()
    r = client.post(
        "/api/platform/entropy-complexity",
        json={"series": [1.0] * 50, "bins": 1},
    )
    assert r.status_code == 422


def test_entropy_complexity_endpoint_422_invalid_params():
    client = _request()
    r = client.post(
        "/api/platform/entropy-complexity",
        json={"series": [1.0] * 50, "permutation_order": 1},
    )
    assert r.status_code == 422


def test_entropy_complexity_endpoint_200_all_metrics_in_unit_range():
    # P262 contract: every numeric metric returned by the endpoint lies in [0, 1].
    series = [float(i) + 0.1 * ((i * 7) % 11) for i in range(150)]
    client = _request()
    r = client.post("/api/platform/entropy-complexity", json={"series": series})
    assert r.status_code == 200, r.text
    body = r.json()
    for key in (
        "shannon_entropy",
        "sample_entropy",
        "permutation_entropy",
        "hurst_exponent",
    ):
        assert key in body, f"missing {key!r}"
        value = body[key]
        assert 0.0 <= value <= 1.0, f"{key}={value} out of [0, 1]"


# ---------------------------------------------------------------------------
# P263 — rolling feature report endpoint
# ---------------------------------------------------------------------------


def test_rolling_features_endpoint_200_with_benchmark():
    series = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    client = _request()
    r = client.post(
        "/api/platform/rolling-features",
        json={"series": series, "window": 3, "alpha": 0.3, "benchmark": series},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("mean", "std", "zscore", "skew", "kurtosis", "ewma", "beta"):
        assert key in body, f"missing field {key!r}"
    assert len(body["mean"]) == len(series)
    # First window-1 entries of the rolling stats are None.
    assert body["mean"][0] is None and body["mean"][1] is None
    assert body["std"][0] is None and body["std"][1] is None
    assert body["zscore"][0] is None and body["zscore"][1] is None
    # ewma has no warm-up.
    assert body["ewma"][0] == pytest.approx(1.0)
    # beta is populated (benchmark provided) and first window-1 are None.
    assert body["beta"] is not None
    assert body["beta"][2] == pytest.approx(1.0, rel=1e-9, abs=1e-9)


def test_rolling_features_endpoint_200_no_benchmark():
    series = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    client = _request()
    r = client.post(
        "/api/platform/rolling-features",
        json={"series": series, "window": 3},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # No benchmark ⇒ beta is None.
    assert body["beta"] is None
    # mean / std / zscore / ewma keys all present.
    for key in ("mean", "std", "zscore", "ewma"):
        assert key in body and len(body[key]) == len(series)


def test_rolling_features_endpoint_422_empty_series():
    client = _request()
    r = client.post("/api/platform/rolling-features", json={"series": []})
    assert r.status_code == 422


def test_rolling_features_endpoint_422_missing_series():
    client = _request()
    r = client.post("/api/platform/rolling-features", json={})
    assert r.status_code == 422


def test_rolling_features_endpoint_422_invalid_window():
    client = _request()
    r = client.post(
        "/api/platform/rolling-features",
        json={"series": [1.0, 2.0, 3.0], "window": 1},
    )
    assert r.status_code == 422


def test_rolling_features_endpoint_422_window_too_large():
    client = _request()
    r = client.post(
        "/api/platform/rolling-features",
        json={"series": [1.0, 2.0, 3.0], "window": 10},
    )
    assert r.status_code == 422


def test_rolling_features_endpoint_422_invalid_alpha():
    client = _request()
    r = client.post(
        "/api/platform/rolling-features",
        json={"series": [1.0, 2.0, 3.0], "alpha": 0.0},
    )
    assert r.status_code == 422


def test_rolling_features_endpoint_422_benchmark_length_mismatch():
    client = _request()
    r = client.post(
        "/api/platform/rolling-features",
        json={"series": [1.0, 2.0, 3.0], "benchmark": [1.0, 2.0]},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P264 — factor IC endpoint
# ---------------------------------------------------------------------------


def test_factor_ic_endpoint_200():
    client = _request()
    n = 20
    factor = [float(i) for i in range(n)]
    forward_returns = [0.001 * i + 1e-6 * (i % 3) for i in range(n)]
    r = client.post(
        "/api/platform/factor-ic",
        json={"factor": factor, "forward_returns": forward_returns, "n_quantiles": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "pearson_ic" in body and "spearman_ic" in body
    assert "buckets" in body and "quantile_spread" in body
    assert len(body["buckets"]) == 5
    assert body["pearson_ic"] > 0.95
    assert body["quantile_spread"] > 0.0


def test_factor_ic_endpoint_422_length_mismatch():
    client = _request()
    r = client.post(
        "/api/platform/factor-ic",
        json={"factor": [1.0, 2.0, 3.0], "forward_returns": [0.01, 0.02]},
    )
    assert r.status_code == 422


def test_factor_ic_endpoint_422_missing_factor():
    client = _request()
    r = client.post(
        "/api/platform/factor-ic",
        json={"forward_returns": [0.01, 0.02, 0.03]},
    )
    assert r.status_code == 422


def test_factor_ic_endpoint_422_invalid_n_quantiles():
    client = _request()
    r = client.post(
        "/api/platform/factor-ic",
        json={
            "factor": [1.0, 2.0, 3.0],
            "forward_returns": [0.01, 0.02, 0.03],
            "n_quantiles": 5,
        },
    )
    assert r.status_code == 422


def test_factor_ic_endpoint_422_empty():
    client = _request()
    r = client.post(
        "/api/platform/factor-ic",
        json={"factor": [], "forward_returns": []},
    )
    assert r.status_code == 422


def test_factor_ic_endpoint_422_bool_entry():
    client = _request()
    r = client.post(
        "/api/platform/factor-ic",
        json={"factor": [1.0, 2.0, True], "forward_returns": [0.01, 0.02, 0.03]},
    )
    assert r.status_code == 422


def test_factor_ic_endpoint_422_non_finite():
    client = _request()
    # httpx's TestClient rejects NaN/inf at request-serialization time
    # (allow_nan=False), so emit the body via json.dumps(allow_nan=True) —
    # which writes a raw ``NaN`` token — and post it as content. The
    # server's validator must reject it with 422.
    payload = {
        "factor": [1.0, float("nan"), 3.0],
        "forward_returns": [0.01, 0.02, 0.03],
    }
    r = client.post(
        "/api/platform/factor-ic",
        content=json.dumps(payload, allow_nan=True),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P265 — feature orthogonalization endpoint
# ---------------------------------------------------------------------------


def test_feature_orthogonalization_endpoint_200():
    payload = {
        "panel": {
            "A": [1.0, 2.0, 3.0, 4.0, 5.0],
            "B": [2.0, 4.0, 6.0, 8.0, 10.0],  # dup of A → dropped
            "C": [1.0, -1.0, 1.0, -1.0, 1.0],
        },
        "threshold": 0.95,
    }
    client = _request()
    r = client.post("/api/platform/feature-orthogonalization", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "kept_features" in body and "dropped_features" in body
    assert "A" in body["kept_features"]
    assert "C" in body["kept_features"]
    assert body["dropped_features"] == ["B"]
    assert "A" in body["vif_scores"]
    assert "B" in body["vif_scores"]
    # correlations has pair labels like "A|B".
    assert any("|" in label for label in body["correlations"].keys())
    assert body["residualized"] is None


def test_feature_orthogonalization_endpoint_200_with_target():
    payload = {
        "panel": {"A": [1.0, 2.0, 3.0, 4.0, 5.0], "B": [1.0, -1.0, 1.0, -1.0, 1.0]},
        "target": [2.0, 4.0, 6.0, 8.0, 10.0],
        "threshold": 0.95,
    }
    client = _request()
    r = client.post("/api/platform/feature-orthogonalization", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["residualized"] is not None
    assert len(body["residualized"]) == 5


def test_feature_orthogonalization_endpoint_422_empty_panel():
    client = _request()
    r = client.post("/api/platform/feature-orthogonalization", json={"panel": {}})
    assert r.status_code == 422


def test_feature_orthogonalization_endpoint_422_missing_panel():
    client = _request()
    r = client.post("/api/platform/feature-orthogonalization", json={})
    assert r.status_code == 422


def test_feature_orthogonalization_endpoint_422_invalid_threshold():
    payload = {"panel": {"A": [1.0, 2.0, 3.0]}, "threshold": 1.5}
    client = _request()
    r = client.post("/api/platform/feature-orthogonalization", json=payload)
    assert r.status_code == 422


def test_feature_orthogonalization_endpoint_422_bool_entry():
    payload = {
        "panel": {"A": [1.0, True, 3.0], "B": [1.0, 2.0, 3.0]},
    }
    client = _request()
    r = client.post("/api/platform/feature-orthogonalization", json=payload)
    assert r.status_code == 422


def test_feature_orthogonalization_endpoint_422_length_mismatch():
    payload = {
        "panel": {"A": [1.0, 2.0], "B": [1.0, 2.0, 3.0]},
    }
    client = _request()
    r = client.post("/api/platform/feature-orthogonalization", json=payload)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P266 — signal-combination endpoint
# ---------------------------------------------------------------------------


def test_signal_combination_endpoint_200_zscore_default():
    payload = {
        "signals": {
            "a": [1.0, 2.0, 3.0, 4.0, 5.0],
            "b": [5.0, 4.0, 3.0, 2.0, 1.0],
        },
    }
    client = _request()
    r = client.post("/api/platform/signal-combination", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"combined", "weights", "standardized", "method", "n_signals"}
    assert body["method"] == "zscore"
    assert body["n_signals"] == 2
    assert len(body["combined"]) == 5
    assert set(body["weights"].keys()) == {"a", "b"}
    assert set(body["standardized"].keys()) == {"a", "b"}
    # equal weights → |w| sums to 1
    assert abs(sum(abs(v) for v in body["weights"].values()) - 1.0) < 1e-9


def test_signal_combination_endpoint_200_explicit_weights_and_rank():
    payload = {
        "signals": {"a": [1.0, 2.0, 3.0], "b": [3.0, 2.0, 1.0]},
        "weights": {"a": 2.0, "b": -2.0},
        "method": "raw",
    }
    client = _request()
    r = client.post("/api/platform/signal-combination", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "raw"
    assert abs(body["weights"]["a"] - 0.5) < 1e-9
    assert abs(body["weights"]["b"] + 0.5) < 1e-9


def test_signal_combination_endpoint_422_empty_signals():
    client = _request()
    r = client.post("/api/platform/signal-combination", json={"signals": {}})
    assert r.status_code == 422


def test_signal_combination_endpoint_422_invalid_method():
    payload = {"signals": {"a": [1.0, 2.0]}, "method": "bogus"}
    client = _request()
    r = client.post("/api/platform/signal-combination", json=payload)
    assert r.status_code == 422


def test_signal_combination_endpoint_422_weights_mismatch():
    payload = {
        "signals": {"a": [1.0, 2.0], "b": [3.0, 4.0]},
        "weights": {"a": 1.0},  # missing 'b'
    }
    client = _request()
    r = client.post("/api/platform/signal-combination", json=payload)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P267 — backtest diagnostics
# ---------------------------------------------------------------------------


def test_backtest_diagnostics_endpoint_200_returns_full_report():
    payload = {
        "trades": [1.0, -0.5, 2.0, -1.5, 0.5, 0.8, -0.3, 1.2, -0.7, 0.4],
        "n_bootstrap": 200,
        "seed": 42,
    }
    client = _request()
    r = client.post("/api/platform/backtest-diagnostics", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    for key in (
        "expectancy",
        "profit_factor",
        "payoff_ratio",
        "win_rate",
        "loss_rate",
        "max_win_streak",
        "max_loss_streak",
        "bootstrap_expectancy_ci",
        "n_trades",
    ):
        assert key in body
    # expectancy / profit_factor / bootstrap_expectancy_ci must be populated.
    assert isinstance(body["expectancy"], float)
    assert isinstance(body["profit_factor"], (int, float))
    bsci = body["bootstrap_expectancy_ci"]
    assert set(bsci.keys()) == {"low", "high", "seed", "n_bootstrap"}
    assert bsci["seed"] == 42
    assert bsci["n_bootstrap"] == 200
    assert bsci["low"] <= bsci["high"]
    assert body["n_trades"] == len(payload["trades"])


def test_backtest_diagnostics_endpoint_422_empty_trades():
    client = _request()
    r = client.post("/api/platform/backtest-diagnostics", json={"trades": []})
    assert r.status_code == 422


def test_backtest_diagnostics_endpoint_422_missing_trades():
    client = _request()
    r = client.post("/api/platform/backtest-diagnostics", json={})
    assert r.status_code == 422


def test_backtest_diagnostics_endpoint_422_invalid_n_bootstrap():
    client = _request()
    r = client.post(
        "/api/platform/backtest-diagnostics",
        json={"trades": [1.0, -0.5], "n_bootstrap": True},
    )
    assert r.status_code == 422


def test_backtest_diagnostics_endpoint_422_invalid_seed():
    client = _request()
    r = client.post(
        "/api/platform/backtest-diagnostics",
        json={"trades": [1.0, -0.5], "seed": True},
    )
    assert r.status_code == 422


def test_backtest_diagnostics_endpoint_no_loss_trades_returns_json_safe_infinity():
    # Legitimate "no-loss" edge: every trade is a win, so profit_factor and
    # payoff_ratio are math.inf in the pure function. The HTTP layer must NOT
    # 500 — FastAPI's default JSON encoder rejects non-finite floats. The
    # contract is that the endpoint serializes these to the JSON convention
    # string "Infinity" so the response body remains valid JSON.
    client = _request()
    r = client.post(
        "/api/platform/backtest-diagnostics",
        json={"trades": [1.0, 2.0], "n_bootstrap": 10, "seed": 0},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # profit_factor / payoff_ratio are the non-finite fields on a no-loss edge.
    assert body["profit_factor"] == "Infinity"
    assert body["payoff_ratio"] == "Infinity"
    # Sanity: the finite fields are still plain JSON numbers.
    assert isinstance(body["expectancy"], float)
    assert body["expectancy"] == pytest.approx(1.5)
    assert body["n_trades"] == 2


# ---------------------------------------------------------------------------
# P268 — OHLCV data-quality diagnostics endpoint
# ---------------------------------------------------------------------------


def test_data_quality_endpoint_200_clean():
    client = _request()
    bars = [
        {"timestamp": 1_700_000_000 + i * 60, "open": 100.0 + i, "high": 101.0 + i,
         "low": 99.0 + i, "close": 100.5 + i, "volume": 1000}
        for i in range(5)
    ]
    r = client.post("/api/platform/data-quality", json={"bars": bars})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["n_bars"] == 5
    assert body["issue_count"] == 0
    assert body["critical_count"] == 0
    assert body["warning_count"] == 0
    assert body["issues"] == []
    assert body["is_clean"] is True


def test_data_quality_endpoint_200_with_issues():
    client = _request()
    base = 1_700_000_000
    bars = [
        {"timestamp": base, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
        {"timestamp": base + 60, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
        {"timestamp": base + 60, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
        {"timestamp": base + 120, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
        {"timestamp": base + 180, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
    ]
    r = client.post(
        "/api/platform/data-quality",
        json={"bars": bars, "stale_window": 3, "jump_threshold": 0.2},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_clean"] is False
    assert body["issue_count"] == len(body["issues"])
    assert body["issue_count"] > 0
    assert body["critical_count"] >= 1  # duplicate timestamp
    assert all(
        set(i.keys()) == {"index", "field", "severity", "message"} for i in body["issues"]
    )


def test_data_quality_endpoint_422_bars_not_list():
    client = _request()
    r = client.post("/api/platform/data-quality", json={"bars": "not a list"})
    assert r.status_code == 422


def test_data_quality_endpoint_422_bars_empty():
    client = _request()
    r = client.post("/api/platform/data-quality", json={"bars": []})
    assert r.status_code == 422


def test_data_quality_endpoint_422_missing_bars():
    client = _request()
    r = client.post("/api/platform/data-quality", json={})
    assert r.status_code == 422


def test_data_quality_endpoint_422_bar_missing_field():
    client = _request()
    r = client.post(
        "/api/platform/data-quality",
        json={"bars": [{"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}]},
    )
    assert r.status_code == 422


def test_data_quality_endpoint_422_non_finite_price():
    client = _request()
    # httpx's TestClient rejects NaN/inf at request-serialization time
    # (allow_nan=False), so emit the body as a raw JSON literal with a ``NaN``
    # token and post it as content. The server's validator must reject it.
    body = (
        '{"bars": [{"timestamp": 1.0, "open": 1.0, "high": 2.0, '
        '"low": 0.5, "close": NaN}]}'
    )
    r = client.post(
        "/api/platform/data-quality",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_data_quality_endpoint_422_invalid_stale_window():
    client = _request()
    bars = [
        {"timestamp": 1.0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
        {"timestamp": 2.0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
    ]
    r = client.post("/api/platform/data-quality", json={"bars": bars, "stale_window": 0})
    assert r.status_code == 422


def test_data_quality_endpoint_422_invalid_jump_threshold():
    client = _request()
    bars = [
        {"timestamp": 1.0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
        {"timestamp": 2.0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
    ]
    r = client.post("/api/platform/data-quality", json={"bars": bars, "jump_threshold": -0.1})
    assert r.status_code == 422


def test_data_quality_endpoint_422_bar_not_dict():
    # A bar shaped as a JSON array of key/value pairs is NOT a dict. The old
    # implementation coerced it with ``dict(b)``, silently producing a valid
    # bar dict and bypassing the strict-schema validation. The endpoint must
    # reject any non-dict bar with 422.
    client = _request()
    bars = [
        [
            ["timestamp", 1],
            ["open", 1],
            ["high", 1],
            ["low", 1],
            ["close", 1],
        ]
    ]
    r = client.post("/api/platform/data-quality", json={"bars": bars})
    assert r.status_code == 422, r.text


def test_data_quality_endpoint_422_invalid_expected_interval():
    client = _request()
    bars = [
        {"timestamp": 1.0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
        {"timestamp": 2.0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
    ]
    r = client.post(
        "/api/platform/data-quality",
        json={"bars": bars, "expected_interval_seconds": -1.0},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# P269–P278 — factor research and strategy diagnostics endpoints
# ---------------------------------------------------------------------------


def test_factor_turnover_endpoint_200_and_422():
    client = _request()
    r = client.post(
        "/api/platform/factor-turnover",
        json={"snapshots": [{"A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0}, {"A": 4.2, "B": 2.8, "C": 2.1, "D": 1.2}], "bucket_fraction": 0.5},
    )
    assert r.status_code == 200, r.text
    assert r.json()["bucket_size"] == 2
    assert client.post("/api/platform/factor-turnover", json={"snapshots": []}).status_code == 422


def test_factor_decay_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/factor-decay", json={"factor": [1, 2, 3], "forward_returns": {"1": [0.1, 0.2, 0.3], "2": [0.3, 0.2, 0.1]}})
    assert r.status_code == 200, r.text
    assert r.json()["best_horizon"] == "1"
    assert client.post("/api/platform/factor-decay", json={"factor": [1], "forward_returns": {}}).status_code == 422


def test_factor_quantiles_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/factor-quantiles", json={"factor": [1, 2, 3, 4], "forward_returns": [0.1, 0.2, 0.3, 0.4], "n_quantiles": 2})
    assert r.status_code == 200, r.text
    assert r.json()["top_bottom_spread"] > 0
    assert client.post("/api/platform/factor-quantiles", json={"factor": [1], "forward_returns": [1]}).status_code == 422


def test_ic_diagnostics_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/ic-diagnostics", json={"ic_series": [0.1, -0.05, 0.02]})
    assert r.status_code == 200, r.text
    assert "mean_ic" in r.json()
    assert client.post("/api/platform/ic-diagnostics", json={"ic_series": [0.1]}).status_code == 422


def test_factor_data_quality_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/factor-data-quality", json={"panel": {"value": [1.0, None, 2.0], "quality": [1.0, 1.0, 1.0]}})
    assert r.status_code == 200, r.text
    assert r.json()["feature_count"] == 2
    assert client.post("/api/platform/factor-data-quality", json={"panel": {}}).status_code == 422


def test_signal_persistence_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/signal-persistence", json={"signal": [1, 1, 0.9, 0.8], "max_lag": 2})
    assert r.status_code == 200, r.text
    assert "autocorrelation" in r.json()
    assert client.post("/api/platform/signal-persistence", json={"signal": [1, 2]}).status_code == 422


def test_strategy_quality_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/strategy-quality", json={"trades": [1.0, -0.5, 2.0]})
    assert r.status_code == 200, r.text
    assert "sqn" in r.json()
    assert client.post("/api/platform/strategy-quality", json={"trades": []}).status_code == 422


def test_regime_performance_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/regime-performance", json={"returns": [0.1, -0.1], "regimes": ["bull", "bear"]})
    assert r.status_code == 200, r.text
    assert set(r.json()["regimes"].keys()) == {"bull", "bear"}
    assert client.post("/api/platform/regime-performance", json={"returns": [0.1], "regimes": []}).status_code == 422
    assert client.post("/api/platform/regime-performance", json={"returns": [0.1], "regimes": [1]}).status_code == 422


def test_strategy_diversification_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/strategy-diversification", json={"strategies": {"A": [0.1, -0.1, 0.2], "B": [0.1, -0.1, 0.2]}})
    assert r.status_code == 200, r.text
    assert ["A", "B"] in r.json()["redundant_pairs"]
    assert client.post("/api/platform/strategy-diversification", json={"strategies": {"A": [1]}}).status_code == 422


def test_backtest_confidence_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/backtest-confidence", json={"returns": [0.01, 0.02, -0.01, 0.03], "n_bootstrap": 20, "seed": 3, "window": 2})
    assert r.status_code == 200, r.text
    assert r.json()["ci_low"] <= r.json()["mean_return"] <= r.json()["ci_high"]
    assert client.post("/api/platform/backtest-confidence", json={"returns": [0.1], "window": 2}).status_code == 422
    assert client.post("/api/platform/backtest-confidence", json={"returns": [0.1, 0.2], "n_bootstrap": 10001, "window": 2}).status_code == 422


def test_strategy_quality_endpoint_serializes_non_finite_as_null():
    client = _request()
    r = client.post("/api/platform/strategy-quality", json={"trades": [1.0, 1.0, 1.0]})
    assert r.status_code == 200, r.text
    assert r.json()["sqn"] is None
    assert r.json()["payoff_ratio"] is None


# ---------------------------------------------------------------------------
# P279–P288 — ML research pipeline endpoints
# ---------------------------------------------------------------------------


def test_forecast_diagnostics_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/forecast-diagnostics", json={"predictions": [0.1, -0.2, 0.3], "actuals": [0.1, -0.1, 0.2], "n_buckets": 2})
    assert r.status_code == 200, r.text
    assert "mse" in r.json()
    assert client.post("/api/platform/forecast-diagnostics", json={"predictions": [1], "actuals": [1, 2]}).status_code == 422


def test_triple_barrier_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/triple-barrier-labels", json={"prices": [100, 103], "events": [{"index": 0, "side": "long"}], "profit_take_pct": 0.02, "stop_loss_pct": 0.01, "max_holding_bars": 1})
    assert r.status_code == 200, r.text
    assert r.json()["labels"][0]["label"] == 1
    assert client.post("/api/platform/triple-barrier-labels", json={"prices": [], "events": []}).status_code == 422
    assert client.post("/api/platform/triple-barrier-labels", json={"prices": [0, 1], "events": [{"index": 0}]}).status_code == 422


def test_sample_uniqueness_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/sample-uniqueness", json={"events": [{"id": "a", "start": 0, "end": 1}, {"id": "b", "start": 1, "end": 2}]})
    assert r.status_code == 200, r.text
    assert r.json()["average_uniqueness"] < 1.0
    assert client.post("/api/platform/sample-uniqueness", json={"events": [{"start": 2, "end": 1}]}).status_code == 422
    assert client.post("/api/platform/sample-uniqueness", json={"events": ["bad"]}).status_code == 422


def test_bar_builder_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/bar-builder", json={"mode": "tick", "threshold": 2, "ticks": [{"timestamp": "t1", "price": 1, "volume": 1}, {"timestamp": "t2", "price": 2, "volume": 1}]})
    assert r.status_code == 200, r.text
    assert r.json()["bar_count"] == 1
    assert client.post("/api/platform/bar-builder", json={"mode": "tick", "threshold": 0, "ticks": []}).status_code == 422


def test_factor_neutralization_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/factor-neutralization", json={"factor": {"A": 1, "B": 3}, "method": "market_demean"})
    assert r.status_code == 200, r.text
    assert r.json()["neutralized"]["A"] == -1
    assert client.post("/api/platform/factor-neutralization", json={"factor": {"A": 1}, "method": "group_demean"}).status_code == 422
    assert client.post("/api/platform/factor-neutralization", json={"factor": {"A": 1, "B": 2}, "method": "residualize", "exposures": "AB"}).status_code == 422
    assert client.post("/api/platform/factor-neutralization", json={"factor": {"A": 1, "B": 2}, "method": "residualize", "exposures": {"A": 1, "B": {"x": 2}}}).status_code == 422
    assert client.post("/api/platform/factor-neutralization", json={"factor": {"A": 1, "B": 2}, "method": "residualize", "exposures": {"A": {"x": "nan"}, "B": {"x": 2}}}).status_code == 422


def test_factor_tearsheet_endpoint_200_and_422():
    client = _request()
    records = [{"date": "d1", "symbol": "A", "factor": 1, "forward_return": 0.1}, {"date": "d1", "symbol": "B", "factor": -1, "forward_return": -0.1}]
    r = client.post("/api/platform/factor-tearsheet", json={"records": records, "n_quantiles": 2})
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["mean_rank_ic"] == 1
    assert client.post("/api/platform/factor-tearsheet", json={"records": []}).status_code == 422
    assert client.post("/api/platform/factor-tearsheet", json={"records": [{"date": "d1"}]}).status_code == 422


def test_feature_pipeline_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/feature-pipeline", json={"price_panel": {"A": [1, 2], "B": [2, 1]}, "features": [{"name": "ret", "op": "return", "window": 1}]})
    assert r.status_code == 200, r.text
    assert r.json()["feature_count"] == 1
    assert client.post("/api/platform/feature-pipeline", json={"price_panel": {"A": [1]}, "features": [{"name": "x", "op": "eval"}]}).status_code == 422
    assert client.post("/api/platform/feature-pipeline", json={"price_panel": {"A": [1, 2]}, "features": ["bad"]}).status_code == 422
    assert client.post("/api/platform/feature-pipeline", json={"price_panel": {"A": [1, 2]}, "features": [{"name": "x", "op": "return", "window": 0}]}).status_code == 422


def test_signal_backtest_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/signal-backtest", json={"prices": [100, 105], "entries": [True, False], "exits": [False, True], "size": 1, "initial_cash": 1000})
    assert r.status_code == 200, r.text
    assert r.json()["stats"]["num_trades"] == 1
    assert client.post("/api/platform/signal-backtest", json={"prices": [1, 2], "entries": [True], "exits": [False, True]}).status_code == 422
    assert client.post("/api/platform/signal-backtest", json={"prices": [0, 1], "entries": [True, False], "exits": [False, True]}).status_code == 422
    assert client.post("/api/platform/signal-backtest", json={"prices": [1, 2], "entries": [1, 0], "exits": [False, True]}).status_code == 422
    assert client.post("/api/platform/signal-backtest", json={"prices": [1, 2], "entries": [True, False], "exits": [False, True], "initial_cash": 0}).status_code == 422


def test_rolling_tearsheet_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/rolling-tearsheet", json={"returns": [0.01, -0.01, 0.02], "windows": [2]})
    assert r.status_code == 200, r.text
    assert "2" in r.json()["windows"]
    daily = client.post("/api/platform/rolling-tearsheet", json={"returns": [0.01, 0.02, 0.03], "windows": [3], "periods_per_year": 252})
    raw = client.post("/api/platform/rolling-tearsheet", json={"returns": [0.01, 0.02, 0.03], "windows": [3], "periods_per_year": 1})
    assert daily.status_code == 200 and raw.status_code == 200
    assert daily.json()["windows"]["3"]["rolling_sharpe"][2] > raw.json()["windows"]["3"]["rolling_sharpe"][2]
    daily_alpha = client.post("/api/platform/rolling-tearsheet", json={"returns": [0.02, 0.025, 0.04], "benchmark": [0.005, 0.01, 0.015], "windows": [3], "periods_per_year": 252})
    raw_alpha = client.post("/api/platform/rolling-tearsheet", json={"returns": [0.02, 0.025, 0.04], "benchmark": [0.005, 0.01, 0.015], "windows": [3], "periods_per_year": 1})
    assert daily_alpha.status_code == 200 and raw_alpha.status_code == 200
    assert abs(raw_alpha.json()["windows"]["3"]["rolling_alpha"][2]) > 1e-9
    assert daily_alpha.json()["windows"]["3"]["rolling_alpha"][2] == pytest.approx(raw_alpha.json()["windows"]["3"]["rolling_alpha"][2] * 252)
    assert client.post("/api/platform/rolling-tearsheet", json={"returns": [0.01], "windows": [2]}).status_code == 422
    assert client.post("/api/platform/rolling-tearsheet", json={"returns": [0.01, 0.02], "windows": [2], "periods_per_year": 0}).status_code == 422
    assert client.post("/api/platform/rolling-tearsheet", json={"returns": [0.01, 0.02], "windows": [2.9]}).status_code == 422


def test_portfolio_constraints_endpoint_200_and_422():
    client = _request()
    r = client.post("/api/platform/portfolio-constraints", json={"weights": {"A": 0.7}, "constraints": {"max_position_weight": 0.6}})
    assert r.status_code == 200, r.text
    assert r.json()["passed"] is False
    assert client.post("/api/platform/portfolio-constraints", json={"weights": {}}).status_code == 422
    assert client.post("/api/platform/portfolio-constraints", json={"weights": {"A": 0.5}, "groups": "bad"}).status_code == 422
