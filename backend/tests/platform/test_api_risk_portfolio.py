"""Tests for P211 / P212 risk-metrics and portfolio-optimize endpoints."""

from __future__ import annotations

import os

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
