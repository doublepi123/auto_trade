from __future__ import annotations

import random
from datetime import datetime, timezone
from decimal import Decimal

from app.platform.smart_optimizer import SmartOptimizer, _quasi_random_samples


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


def test_quasi_random_samples_count_and_keys():
    rng = random.Random(1)
    samples = _quasi_random_samples({"buy_low": [145, 146], "sell_high": [154, 155]}, 5, rng)
    assert len(samples) == 5
    assert all(set(s.keys()) == {"buy_low", "sell_high"} for s in samples)
    assert all(s["buy_low"] in (145, 146) for s in samples)


def test_search_returns_ranked_and_prunes():
    bars = [_bar(144, i) if i % 2 == 0 else _bar(156, i) for i in range(8)]
    opt = SmartOptimizer(seed=7)
    result = opt.search(
        strategy_name="interval",
        param_choices={"buy_low": [145, 146, 147], "sell_high": [153, 154, 155], "quantity": [10]},
        symbols=["AAPL.US"],
        bars=bars,
        metric="sharpe",
        num_trials=8,
        top_k=3,
        initial_cash=Decimal("10000"),
    )
    assert result["num_trials"] == 8
    assert result["survivors"] == 4  # halved
    assert len(result["ranked"]) <= 3
    # ranked descending by metric
    vals = [r["sharpe"] for r in result["ranked"]]
    assert vals == sorted(vals, reverse=True)
    assert all("params" in r and "coarse_score" in r for r in result["ranked"])


def test_search_deterministic_with_seed():
    bars = [_bar(144, i) if i % 2 == 0 else _bar(156, i) for i in range(6)]
    a = SmartOptimizer(seed=42).search(
        "interval",
        {"buy_low": [145, 146], "sell_high": [154, 155], "quantity": [10]},
        ["AAPL.US"],
        bars,
        num_trials=6,
        top_k=3,
        initial_cash=Decimal("10000"),
    )
    b = SmartOptimizer(seed=42).search(
        "interval",
        {"buy_low": [145, 146], "sell_high": [154, 155], "quantity": [10]},
        ["AAPL.US"],
        bars,
        num_trials=6,
        top_k=3,
        initial_cash=Decimal("10000"),
    )
    assert a == b


def test_search_empty_choices():
    bars = [_bar(144, 0)]
    result = SmartOptimizer().search(
        "interval",
        {},
        ["AAPL.US"],
        bars,
        num_trials=5,
        top_k=3,
        initial_cash=Decimal("10000"),
    )
    assert result["ranked"] == []
    assert result["num_trials"] == 0
