"""P331: adjusted Sharpe ratio tests."""

from __future__ import annotations

import pytest


def test_autocorr_adjusted_lt_raw_sharpe():
    """Positive autocorrelation → adjusted_sharpe < raw_sharpe."""
    from app.platform.adjusted_sharpe import adjusted_sharpe_report

    import math
    import random

    rng = random.Random(42)
    # Build a series with positive lag-1 autocorrelation: x[t] = 0.5*x[t-1] + noise
    n = 500
    returns = []
    prev = 0.0
    for _ in range(n):
        r = 0.5 * prev + rng.gauss(0.0002, 0.01)
        returns.append(r)
        prev = r

    result = adjusted_sharpe_report(returns, periods_per_year=252, max_lag=10).to_dict()
    assert result["raw_sharpe"] > 0
    # Positive autocorrelation inflates raw Sharpe → corrected should be lower
    assert result["autocorr_adjusted"] < result["raw_sharpe"]
    assert result["skewness"] is not None
    assert result["kurtosis"] is not None
    assert len(result["autocorrelations"]) == 10


def test_adjusted_sharpe_rejects_short_series():
    from app.platform.adjusted_sharpe import adjusted_sharpe_report

    with pytest.raises(ValueError, match="at least 2"):
        adjusted_sharpe_report([0.01], periods_per_year=252, max_lag=5)


def test_adjusted_sharpe_rejects_non_finite():
    from app.platform.adjusted_sharpe import adjusted_sharpe_report

    with pytest.raises(ValueError, match="finite"):
        adjusted_sharpe_report([0.01, float("inf")], periods_per_year=252, max_lag=5)


def test_adjusted_sharpe_constant_returns():
    """All-zero returns → Sharpe = 0."""
    from app.platform.adjusted_sharpe import adjusted_sharpe_report

    result = adjusted_sharpe_report([0.0] * 100, periods_per_year=252, max_lag=5).to_dict()
    assert result["raw_sharpe"] == 0.0
    assert result["autocorr_adjusted"] == 0.0
    assert result["moments_adjusted"] == 0.0
