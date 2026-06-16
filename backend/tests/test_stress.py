"""What-If / Stress ensemble — engine + API. No DB needed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

import pytest

from app.core.backtest import BacktestBar, BacktestEngineParams, stress_test
from app.main import app

client = TestClient(app)


def make_bars(n: int) -> list[BacktestBar]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        BacktestBar(
            timestamp=base + timedelta(minutes=i),
            open=150, high=205, low=95, close=150, volume=1000,
        )
        for i in range(n)
    ]


class TestStressEngine:
    def test_returns_distribution(self) -> None:
        result = stress_test(
            BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2),
            make_bars(12),
            scenarios=20,
            jitter_pct=2.0,
            seed=7,
        )
        assert result.scenarios_run == 20
        assert len(result.returns) == 20
        assert result.returns == sorted(result.returns)
        assert result.median_return_pct is not None
        assert result.p5_return_pct is not None
        assert result.p95_return_pct is not None
        assert result.p5_return_pct <= result.median_return_pct <= result.p95_return_pct

    def test_deterministic_with_seed(self) -> None:
        params = BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2)
        bars = make_bars(12)
        r1 = stress_test(params, bars, scenarios=15, jitter_pct=2.0, seed=42)
        r2 = stress_test(params, bars, scenarios=15, jitter_pct=2.0, seed=42)
        assert r1.returns == r2.returns

    def test_zero_jitter_equals_baseline(self) -> None:
        params = BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2)
        result = stress_test(params, make_bars(10), scenarios=5, jitter_pct=0.0, seed=1)
        assert result.baseline_return_pct is not None
        # No jitter -> every scenario is identical to baseline.
        assert all(abs(r - result.baseline_return_pct) < 1e-9 for r in result.returns)
        assert result.median_return_pct == pytest.approx(result.baseline_return_pct)

    def test_profitable_pct_bounds(self) -> None:
        result = stress_test(
            BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2),
            make_bars(12), scenarios=10, jitter_pct=1.0, seed=3,
        )
        assert result.profitable_scenario_pct is not None
        assert 0 <= result.profitable_scenario_pct <= 100

    def test_invalid_inputs(self) -> None:
        params = BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200)
        bars = make_bars(10)
        with pytest.raises(ValueError, match="at least one price bar"):
            stress_test(params, [], scenarios=5)
        with pytest.raises(ValueError, match="scenarios must be at least 1"):
            stress_test(params, bars, scenarios=0)
        with pytest.raises(ValueError, match="scenarios cannot exceed 1000"):
            stress_test(params, bars, scenarios=1001)
        with pytest.raises(ValueError, match="jitter_pct"):
            stress_test(params, bars, scenarios=5, jitter_pct=-1)


_CSV = (
    "timestamp,open,high,low,close,volume\n"
    + "\n".join(
        f"2026-01-01T00:{i:02d}:00Z,150,205,95,150,1000" for i in range(12)
    )
    + "\n"
)


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "base": {"buy_low": 100, "sell_high": 200, "quantity": 2},
        "scenarios": 20,
        "jitter_pct": 2.0,
        "seed": 7,
        "csv_text": _CSV,
    }
    body.update(overrides)
    return body


class TestStressAPI:
    def test_endpoint_success(self) -> None:
        resp = client.post("/api/backtest/stress", json=_body())
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["scenarios_run"] == 20
        assert "returns" in data and len(data["returns"]) == 20

    def test_endpoint_zero_scenarios_422(self) -> None:
        resp = client.post("/api/backtest/stress", json=_body(scenarios=0))
        assert resp.status_code == 422

    def test_endpoint_missing_source_422(self) -> None:
        body = _body()
        body.pop("csv_text")
        resp = client.post("/api/backtest/stress", json=body)
        assert resp.status_code == 422
