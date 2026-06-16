"""Walk-forward rolling-window backtest — engine + API. No DB needed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.core.backtest import BacktestBar, BacktestEngineParams, walk_forward_backtest
from app.main import app

client = TestClient(app)


def make_bars(n: int) -> list[BacktestBar]:
    """n bars oscillating low=95/high=205 so buy_low=100/sell_high=200 trades
    on roughly every other bar — gives each window real, varying metrics."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars: list[BacktestBar] = []
    for i in range(n):
        bars.append(BacktestBar(
            timestamp=base + timedelta(minutes=i),
            open=150, high=205, low=95, close=150, volume=1000,
        ))
    return bars


class TestWalkForwardEngine:
    def test_multiple_windows_with_optimization(self) -> None:
        result = walk_forward_backtest(
            BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2),
            {"buy_low": [95.0, 100.0]},
            make_bars(20),
            train_size=6,
            test_size=4,
        )
        assert result.summary.window_count >= 2
        assert all(w.best_params is not None for w in result.windows)
        assert all(w.test_metrics is not None for w in result.windows)

    def test_empty_grid_is_rolling_evaluation(self) -> None:
        base = BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2)
        result = walk_forward_backtest(base, {}, make_bars(16), train_size=6, test_size=4)
        assert result.summary.window_count >= 1
        # No grid -> best_params is just base every window.
        for w in result.windows:
            assert w.best_params is not None
            assert w.best_params.buy_low == base.buy_low

    def test_step_reduces_window_count(self) -> None:
        base = BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2)
        bars = make_bars(20)
        dense = walk_forward_backtest(base, {}, bars, train_size=6, test_size=2, step=1)
        sparse = walk_forward_backtest(base, {}, bars, train_size=6, test_size=2, step=4)
        assert dense.summary.window_count > sparse.summary.window_count

    def test_summary_aggregation(self) -> None:
        result = walk_forward_backtest(
            BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2),
            {},
            make_bars(18),
            train_size=6,
            test_size=3,
            sort_by="total_return_pct",
        )
        s = result.summary
        returns = [w.test_metrics.total_return_pct for w in result.windows if w.test_metrics]  # type: ignore[union-attr]
        if returns:
            assert s.mean_test_return_pct == pytest.approx(sum(returns) / len(returns))
            assert s.test_return_std_pct is not None
            assert 0 <= s.profitable_window_pct <= 100

    def test_too_few_bars_yields_zero_windows(self) -> None:
        result = walk_forward_backtest(
            BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200),
            {},
            make_bars(5),
            train_size=6,
            test_size=4,
        )
        assert result.summary.window_count == 0
        assert result.summary.mean_test_return_pct is None

    def test_invalid_sizes_raise(self) -> None:
        base = BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200)
        with pytest.raises(ValueError, match="train_size"):
            walk_forward_backtest(base, {}, make_bars(20), train_size=1, test_size=4)
        with pytest.raises(ValueError, match="test_size"):
            walk_forward_backtest(base, {}, make_bars(20), train_size=6, test_size=0)
        with pytest.raises(ValueError, match="step"):
            walk_forward_backtest(base, {}, make_bars(20), train_size=6, test_size=4, step=0)
        with pytest.raises(ValueError, match="sort_by"):
            walk_forward_backtest(base, {}, make_bars(20), train_size=6, test_size=4, sort_by="bogus")

    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one price bar"):
            walk_forward_backtest(
                BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200),
                {}, [], train_size=6, test_size=4,
            )


_CSV = (
    "timestamp,open,high,low,close,volume\n"
    + "\n".join(
        f"2026-01-{(i // 1440) + 1:02d}T{(i % 1440) // 60:02d}:{(i % 1440) % 60:02d}:00Z,150,205,95,150,1000"
        for i in range(20)
    )
    + "\n"
)


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "base": {"buy_low": 100, "sell_high": 200, "quantity": 2},
        "grid": {"buy_low": {"values": [95.0, 100.0]}},
        "train_size": 6,
        "test_size": 4,
        "csv_text": _CSV,
    }
    body.update(overrides)
    return body


class TestWalkForwardAPI:
    def test_endpoint_success(self) -> None:
        resp = client.post("/api/backtest/walk-forward", json=_body())
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["train_size"] == 6
        assert data["summary"]["window_count"] >= 1
        assert isinstance(data["windows"], list)

    def test_endpoint_empty_grid_rolling(self) -> None:
        resp = client.post("/api/backtest/walk-forward", json=_body(grid={}))
        assert resp.status_code == 200, resp.text

    def test_endpoint_bad_train_size_422(self) -> None:
        resp = client.post("/api/backtest/walk-forward", json=_body(train_size=1))
        assert resp.status_code == 422

    def test_endpoint_missing_source_422(self) -> None:
        body = _body()
        body.pop("csv_text")
        resp = client.post("/api/backtest/walk-forward", json=body)
        assert resp.status_code == 422

    def test_endpoint_malformed_csv_422(self) -> None:
        resp = client.post(
            "/api/backtest/walk-forward",
            json=_body(csv_text="timestamp,open,high,low,close\n2026-01-01T00:00:00Z,1,2,1,2\n"),
        )
        assert resp.status_code == 422
