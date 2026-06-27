from __future__ import annotations

import pytest

from app.platform.backtest_confidence import backtest_confidence_report


def test_backtest_confidence_is_deterministic_with_seed():
    first = backtest_confidence_report([0.01, 0.02, -0.01, 0.03, -0.02, 0.01], n_bootstrap=50, seed=7, window=3)
    second = backtest_confidence_report([0.01, 0.02, -0.01, 0.03, -0.02, 0.01], n_bootstrap=50, seed=7, window=3)
    assert first.to_dict() == second.to_dict()
    assert first.to_dict()["ci_low"] <= first.to_dict()["mean_return"] <= first.to_dict()["ci_high"]


def test_backtest_confidence_fragility_rises_with_large_loss():
    calm = backtest_confidence_report([0.01, 0.01, 0.0, 0.02, -0.005], n_bootstrap=20, seed=1, window=3)
    fragile = backtest_confidence_report([0.01, 0.01, 0.0, 0.02, -0.20], n_bootstrap=20, seed=1, window=3)
    assert fragile.to_dict()["fragility_score"] > calm.to_dict()["fragility_score"]


def test_backtest_confidence_rejects_invalid_bootstrap_count():
    with pytest.raises(ValueError):
        backtest_confidence_report([0.01, 0.02], n_bootstrap=0)
    with pytest.raises(ValueError):
        backtest_confidence_report([0.01, 0.02], n_bootstrap=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        backtest_confidence_report([0.01, 0.02], n_bootstrap=10001)
