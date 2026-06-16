"""Tests for the Backtest Parameter Sweep Optimizer.

No DB is required — the sweep is a pure in-memory analysis over BacktestEngine,
mirroring test_backtest.py's no-DB approach.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypedDict, cast

import pytest
from fastapi.testclient import TestClient

from app.core.backtest import (
    BacktestBar,
    BacktestEngineParams,
    SWEEP_ALLOWED_GRID_KEYS,
    SWEEP_DEFAULT_MAX_COMBINATIONS,
    expand_numeric_range,
    sweep_backtest,
)
from app.main import app

client = TestClient(app)


def bar(minute: int, open_: float, high: float, low: float, close: float) -> BacktestBar:
    return BacktestBar(
        timestamp=datetime(2026, 5, 22, 10, minute, tzinfo=timezone.utc),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


def make_bars() -> list[BacktestBar]:
    """Six oscillating bars. With buy_low in ~[100,110] and sell_high=200 this
    yields two BUY@buy_low / SELL@200 round trips, so risk-adjusted metrics are
    well defined and vary across buy_low values. buy_low well below the lowest
    low (95) yields zero trades (None metrics)."""
    return [
        bar(0, 150, 160, 99, 105),
        bar(1, 120, 140, 110, 130),
        bar(2, 150, 205, 145, 200),
        bar(3, 180, 190, 120, 130),
        bar(4, 110, 150, 95, 102),
        bar(5, 150, 210, 140, 205),
    ]


def _metric(row: Any, key: str) -> float:
    """Read a possibly-None metric as -inf so None sorts below every real value."""
    val = getattr(row.metrics, key)
    return val if val is not None else float("-inf")


class TestSweepEngine:
    def test_single_axis_returns_ranked_rows(self) -> None:
        result = sweep_backtest(
            BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2),
            {"buy_low": [100.0, 105.0, 110.0]},
            make_bars(),
        )
        assert len(result.rows) == 3
        assert [row.rank for row in result.rows] == [1, 2, 3]
        assert result.best is result.rows[0]
        assert result.best.rank == 1

    def test_best_has_highest_metric_none_last(self) -> None:
        # buy_low=50 is below the lowest low (95) -> zero trades -> None metric.
        result = sweep_backtest(
            BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2),
            {"buy_low": [50.0, 100.0, 110.0]},
            make_bars(),
            sort_by="sharpe_ratio",
        )
        values = [_metric(r, "sharpe_ratio") for r in result.rows]
        assert values == sorted(values, reverse=True)
        # The zero-trade combo must rank last.
        assert result.rows[-1].params.buy_low == 50.0
        assert result.rows[-1].metrics.sharpe_ratio is None

    def test_skips_invalid_buy_low_vs_sell_high_combos(self) -> None:
        result = sweep_backtest(
            BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2),
            {"buy_low": [150.0, 100.0], "sell_high": [120.0, 200.0]},
            make_bars(),
        )
        # Combos: (150,120) invalid, (150,200) ok, (100,120) ok, (100,200) ok -> 3 valid.
        assert result.evaluated_count == 3
        assert result.skipped_count == 1

    def test_two_axis_cartesian_heatmap_covers_grid(self) -> None:
        result = sweep_backtest(
            BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2),
            {"buy_low": [100.0, 110.0], "sell_high": [200.0, 210.0]},
            make_bars(),
        )
        assert len(result.rows) == 4
        cells = {(c.buy_low, c.sell_high) for c in result.heatmap.cells}
        assert cells == {(100.0, 200.0), (100.0, 210.0), (110.0, 200.0), (110.0, 210.0)}

    def test_sort_by_total_return_desc(self) -> None:
        result = sweep_backtest(
            BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2),
            {"buy_low": [100.0, 105.0, 110.0]},
            make_bars(),
            sort_by="total_return_pct",
        )
        returns = [r.metrics.total_return_pct for r in result.rows]
        assert returns == sorted(returns, reverse=True)

    def test_unknown_grid_key_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown grid keys"):
            sweep_backtest(
                BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200),
                {"max_daily_loss": [100.0, 200.0]},  # not in the allowed sweep set
                make_bars(),
            )

    def test_exceeds_max_combinations_raises(self) -> None:
        with pytest.raises(ValueError, match="max_combinations"):
            sweep_backtest(
                BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200),
                {"buy_low": [100.0, 110.0], "sell_high": [200.0, 210.0]},
                make_bars(),
                max_combinations=2,
            )

    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one price bar"):
            sweep_backtest(
                BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200),
                {"buy_low": [100.0]},
                [],
            )

    def test_invalid_sort_by_raises(self) -> None:
        with pytest.raises(ValueError, match="sort_by"):
            sweep_backtest(
                BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200),
                {"buy_low": [100.0]},
                make_bars(),
                sort_by="bogus",
            )

    def test_max_combinations_bounds(self) -> None:
        base = BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200)
        with pytest.raises(ValueError, match="max_combinations"):
            sweep_backtest(base, {"buy_low": [100.0]}, make_bars(), max_combinations=0)
        with pytest.raises(ValueError, match="max_combinations"):
            sweep_backtest(base, {"buy_low": [100.0]}, make_bars(), max_combinations=99_999)

    def test_empty_grid_axis_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            sweep_backtest(
                BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200),
                {"buy_low": []},
                make_bars(),
            )

    def test_determinism(self) -> None:
        base = BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2)
        grid = {"buy_low": [100.0, 105.0, 110.0], "sell_high": [200.0, 210.0]}
        bars = make_bars()
        r1 = sweep_backtest(base, grid, bars)
        r2 = sweep_backtest(base, grid, bars)
        assert [(r.params.buy_low, r.params.sell_high) for r in r1.rows] == [
            (r.params.buy_low, r.params.sell_high) for r in r2.rows
        ]

    def test_heatmap_best_per_cell_collapses_third_axis(self) -> None:
        result = sweep_backtest(
            BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2),
            {"buy_low": [100.0, 110.0], "sell_high": [200.0], "min_profit_amount": [0.0, 5.0]},
            make_bars(),
            sort_by="sharpe_ratio",
        )
        # 2 buy_low x 1 sell_high x 2 min_profit = 4 rows, 2 cells.
        assert len(result.rows) == 4
        cell_map: dict[tuple[float, float], float] = {}
        for r in result.rows:
            key = (r.params.buy_low, r.params.sell_high)
            cell_map[key] = max(cell_map.get(key, float("-inf")), _metric(r, "sharpe_ratio"))
        for cell in result.heatmap.cells:
            got = cell.value if cell.value is not None else float("-inf")
            assert abs(got - cell_map[(cell.buy_low, cell.sell_high)]) < 1e-12

    def test_rows_populate_full_metric_set(self) -> None:
        result = sweep_backtest(
            BacktestEngineParams(symbol="AAPL.US", buy_low=100, sell_high=200, quantity=2),
            {"buy_low": [100.0, 110.0]},
            make_bars(),
        )
        # At least one trading combo must produce a non-None sharpe.
        assert any(r.metrics.sharpe_ratio is not None for r in result.rows)
        # Fields exist on every row (None allowed).
        for r in result.rows:
            assert hasattr(r.metrics, "sortino_ratio")
            assert hasattr(r.metrics, "calmar_ratio")
            assert hasattr(r.metrics, "profit_factor")

    def test_allowed_grid_keys_set(self) -> None:
        assert SWEEP_ALLOWED_GRID_KEYS == frozenset({
            "buy_low", "sell_high", "min_profit_amount",
            "quantity", "fee_rate", "slippage_pct", "stop_loss_pct",
        })

    def test_default_max_combinations_constant(self) -> None:
        assert SWEEP_DEFAULT_MAX_COMBINATIONS == 2000


class TestExpandNumericRange:
    def test_basic_range(self) -> None:
        assert expand_numeric_range(100, 5, 110) == [100.0, 105.0, 110.0]

    def test_inclusive_of_end_with_tolerance(self) -> None:
        # float accumulation must not drop the endpoint.
        vals = expand_numeric_range(0.1, 0.1, 0.3)
        assert vals[-1] == pytest.approx(0.3)

    def test_single_value(self) -> None:
        assert expand_numeric_range(100, 1, 100) == [100.0]

    def test_zero_step_raises(self) -> None:
        with pytest.raises(ValueError, match="step cannot be zero"):
            expand_numeric_range(100, 0, 110)

    def test_negative_step_raises(self) -> None:
        with pytest.raises(ValueError, match="step must be positive"):
            expand_numeric_range(100, -1, 110)


class _SweepRowJson(TypedDict):
    rank: int
    params: dict[str, Any]
    metrics: dict[str, Any]


class _SweepResultJson(TypedDict):
    rows: list[_SweepRowJson]
    best: _SweepRowJson
    evaluated_count: int
    skipped_count: int
    sort_by: str


def _sweep_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "base": {"buy_low": 100, "sell_high": 200, "quantity": 2},
        "grid": {"buy_low": {"range": {"start": 100, "end": 110, "step": 5}}},
        "csv_text": (
            "timestamp,open,high,low,close,volume\n"
            "2026-05-22T10:00:00Z,150,160,99,105,1000\n"
            "2026-05-22T10:01:00Z,120,140,110,130,1200\n"
            "2026-05-22T10:02:00Z,150,205,145,200,1300\n"
            "2026-05-22T10:03:00Z,180,190,120,130,900\n"
            "2026-05-22T10:04:00Z,110,150,98,102,1100\n"
            "2026-05-22T10:05:00Z,150,210,140,202,1250\n"
        ),
    }
    body.update(overrides)
    return body


class TestSweepAPI:
    def test_sweep_endpoint_success(self) -> None:
        resp = client.post("/api/backtest/sweep", json=_sweep_body())
        assert resp.status_code == 200, resp.text
        data = cast(_SweepResultJson, resp.json())
        assert data["sort_by"] == "sharpe_ratio"
        assert data["evaluated_count"] == 3  # 100,105,110
        assert [row["rank"] for row in data["rows"]] == [1, 2, 3]
        assert data["best"]["rank"] == 1
        # Heatmap has one sell_high column -> 3 cells (one per buy_low).
        assert len(resp.json()["heatmap"]["cells"]) == 3

    def test_sweep_endpoint_grid_over_cap_422(self) -> None:
        resp = client.post("/api/backtest/sweep", json=_sweep_body(max_combinations=2))
        assert resp.status_code == 422
        assert "max_combinations" in resp.text

    def test_sweep_endpoint_missing_source_422(self) -> None:
        body = _sweep_body()
        body.pop("csv_text")
        resp = client.post("/api/backtest/sweep", json=body)
        assert resp.status_code == 422

    def test_sweep_endpoint_bad_base_param_422(self) -> None:
        resp = client.post("/api/backtest/sweep", json=_sweep_body(
            base={"buy_low": -1, "sell_high": 200},
        ))
        assert resp.status_code == 422

    def test_sweep_endpoint_malformed_csv_422(self) -> None:
        resp = client.post("/api/backtest/sweep", json=_sweep_body(
            csv_text="timestamp,open,high,low,close\n2026-05-22T10:00:00Z,1,2,1,2\n",
        ))
        assert resp.status_code == 422
        assert "volume" in resp.text

    def test_sweep_endpoint_unknown_grid_key_422(self) -> None:
        resp = client.post("/api/backtest/sweep", json=_sweep_body(
            grid={"max_daily_loss": {"range": {"start": 100, "end": 200, "step": 100}}},
        ))
        assert resp.status_code == 422
        assert "unknown grid keys" in resp.text

    def test_sweep_endpoint_invalid_sort_by_422(self) -> None:
        resp = client.post("/api/backtest/sweep", json=_sweep_body(sort_by="bogus"))
        assert resp.status_code == 422

    def test_sweep_endpoint_empty_grid_422(self) -> None:
        resp = client.post("/api/backtest/sweep", json=_sweep_body(grid={}))
        assert resp.status_code == 422

    def test_sweep_endpoint_values_form(self) -> None:
        resp = client.post("/api/backtest/sweep", json=_sweep_body(
            grid={"buy_low": {"values": [100.0, 110.0]}},
        ))
        assert resp.status_code == 200, resp.text
        assert resp.json()["evaluated_count"] == 2


class TestSweepSchema:
    def test_request_max_combinations_lower_bound(self) -> None:
        from app.schemas import BacktestSweepRequest
        with pytest.raises(Exception):
            BacktestSweepRequest(**_sweep_body(max_combinations=0))

    def test_request_max_combinations_upper_bound(self) -> None:
        from app.schemas import BacktestSweepRequest
        with pytest.raises(Exception):
            BacktestSweepRequest(**_sweep_body(max_combinations=10001))

    def test_request_sort_by_literal(self) -> None:
        from app.schemas import BacktestSweepRequest
        req = BacktestSweepRequest(**_sweep_body(sort_by="calmar_ratio"))
        assert req.sort_by == "calmar_ratio"


class TestRunEndpointSurfacesRatios:
    """The /run response must surface the ratio metrics the engine already
    computes (previously dropped). Additive: existing assertions unaffected."""

    def test_run_response_includes_ratio_fields(self) -> None:
        resp = client.post("/api/backtest/run", json={
            "params": {"buy_low": 100, "sell_high": 200, "quantity": 2, "initial_cash": 10000},
            "csv_text": (
                "timestamp,open,high,low,close,volume\n"
                "2026-05-22T10:00:00Z,150,160,99,105,1000\n"
                "2026-05-22T10:01:00Z,120,140,110,130,1200\n"
                "2026-05-22T10:02:00Z,150,205,145,200,1300\n"
                "2026-05-22T10:03:00Z,180,190,120,130,900\n"
                "2026-05-22T10:04:00Z,110,150,98,102,1100\n"
                "2026-05-22T10:05:00Z,150,210,140,202,1250\n"
            ),
        })
        assert resp.status_code == 200, resp.text
        metrics = resp.json()["metrics"]
        for field in ("sharpe_ratio", "sortino_ratio", "calmar_ratio", "profit_factor", "profit_loss_ratio"):
            assert field in metrics, f"{field} missing from /run metrics"
