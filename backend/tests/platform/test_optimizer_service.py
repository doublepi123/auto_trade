from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.optimizer_service import OptimizerService


def _bar(close, minute):
    return {
        "timestamp": datetime(2026, 6, 23, 10, minute, tzinfo=timezone.utc).isoformat(),
        "symbol": "AAPL.US",
        "open": 150,
        "high": 160,
        "low": 140,
        "close": close,
        "volume": 1000,
    }


def test_grid_search_ranks_by_sharpe_and_returns_all_combos():
    bars = [_bar(144, 0), _bar(156, 1), _bar(144, 2), _bar(156, 3)]
    svc = OptimizerService()
    result = svc.grid_search(
        strategy_name="interval",
        param_grid={"buy_low": [145, 146], "sell_high": [154, 155], "quantity": [10]},
        symbols=["AAPL.US"],
        bars=bars,
        metric="sharpe",
        top_k=5,
        initial_cash=Decimal("10000"),
    )
    assert result["total_combos"] == 4
    assert len(result["ranked"]) <= 4
    # ranked descending by sharpe
    sharpes = [r["sharpe"] for r in result["ranked"]]
    assert sharpes == sorted(sharpes, reverse=True)
    # every ranked entry carries params
    assert all("params" in r for r in result["ranked"])


def test_walk_forward_returns_is_and_oos():
    bars = [_bar(144, i) if i % 2 == 0 else _bar(156, i) for i in range(8)]
    svc = OptimizerService()
    result = svc.walk_forward(
        strategy_name="interval",
        param_grid={"buy_low": [145], "sell_high": [155], "quantity": [10]},
        symbols=["AAPL.US"],
        bars=bars,
        split_fraction=0.5,
        top_k=3,
        initial_cash=Decimal("10000"),
    )
    assert result["split_at"] == 4
    assert len(result["in_sample_ranked"]) >= 1
    assert len(result["out_of_sample"]) == len(result["in_sample_ranked"])
    assert "out_of_sample_sharpe" in result["out_of_sample"][0]
