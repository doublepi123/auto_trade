from __future__ import annotations

import pytest

from app.platform.bootstrap_strategy_significance import bootstrap_strategy_significance_report


def test_bootstrap_significance_returns_pvalue_and_ci():
    returns = [0.01, -0.02, 0.03, 0.015, -0.01, 0.025, -0.015, 0.02, -0.005, 0.012, -0.008, 0.018]
    body = bootstrap_strategy_significance_report(returns, n_bootstrap=200, seed=7).to_dict()
    assert "observed_sharpe" in body
    assert 0.0 <= body["p_value"] <= 1.0
    assert body["ci_lower"] <= body["observed_sharpe"] <= body["ci_upper"]


def test_bootstrap_significance_rejects_short_series():
    with pytest.raises(ValueError):
        bootstrap_strategy_significance_report([0.01])
