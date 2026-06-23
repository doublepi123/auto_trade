from __future__ import annotations

from typing import Any

from app.platform.montecarlo import MonteCarloAnalyzer


def _run(seed: int = 7, **kw: Any) -> dict[str, Any]:
    return MonteCarloAnalyzer(seed=seed).analyze(
        [10.0, -5.0, 20.0, -8.0, 15.0],
        num_simulations=200,
        horizon=5,
        **kw,
    )


def test_deterministic_same_seed() -> None:
    a = _run()
    b = _run()
    assert a == b


def test_prob_loss_range_and_structure() -> None:
    result = _run()
    prob_loss: float = result["prob_loss"]
    assert 0.0 <= prob_loss <= 1.0
    assert "final_pnl" in result
    final_pnl: dict[str, Any] = result["final_pnl"]
    for k in ("p5", "p25", "p50", "p75", "p95"):
        assert k in final_pnl
    assert result["num_simulations"] == 200


def test_ruin_threshold_counts_losses_below() -> None:
    # mostly-winning trades -> prob_ruin with a high negative threshold ~ 0
    result = _run(ruin_threshold=-1000.0)
    assert result["prob_ruin"] == 0.0


def test_empty_pnls_returns_zeros() -> None:
    result = MonteCarloAnalyzer().analyze([], num_simulations=10)
    assert result["prob_loss"] == 0.0
    assert result["final_pnl"]["p50"] == 0.0
