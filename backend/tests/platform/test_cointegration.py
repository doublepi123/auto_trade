"""Tests for P223 cointegration & pairs diagnostics."""

from __future__ import annotations

import math

import pytest

from app.platform.cointegration import (
    cointegration_analysis,
    durbin_watson,
    half_life_ou,
    hedge_ratio_ols,
    spread_series,
    zscore,
)


def test_hedge_ratio_ols_perfect_fit():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [2.0 * xi + 1.0 for xi in x]  # y = 2x + 1
    beta, alpha, r2 = hedge_ratio_ols(y, x)
    assert abs(beta - 2.0) < 1e-9
    assert abs(alpha - 1.0) < 1e-9
    assert abs(r2 - 1.0) < 1e-9


def test_hedge_ratio_ols_length_mismatch():
    with pytest.raises(ValueError):
        hedge_ratio_ols([1.0, 2.0], [1.0])


def test_hedge_ratio_ols_zero_variance():
    with pytest.raises(ValueError):
        hedge_ratio_ols([1.0, 2.0, 3.0], [5.0, 5.0, 5.0])


def test_spread_series():
    s = spread_series([3.0, 5.0], [1.0, 2.0], beta=2.0, alpha=1.0)
    assert s == [0.0, 0.0]  # 3-2*1-1=0, 5-2*2-1=0


def test_durbin_watson_no_autocorr():
    # alternating signs → high DW (~2)
    r = [1.0, -1.0, 1.0, -1.0]
    assert durbin_watson(r) > 1.5


def test_durbin_watson_strong_autocorr():
    # slowly drifting → low DW
    r = [1.0, 1.05, 1.1, 1.15, 1.2]
    assert durbin_watson(r) < 1.0


def test_zscore_full_sample():
    spread = [1.0, 2.0, 3.0, 4.0, 5.0]
    zs = zscore(spread)
    assert len(zs) == 5
    assert abs(zs[-1] - (5.0 - 3.0) / math.sqrt(2.5)) < 1e-9


def test_zscore_zero_variance_returns_zeros():
    zs = zscore([3.0, 3.0, 3.0])
    assert zs == [0.0, 0.0, 0.0]


def test_zscore_window_too_small():
    with pytest.raises(ValueError):
        zscore([1.0, 2.0], window=1)


def test_half_life_ou_mean_reverting():
    # Synthetic AR(1) with kappa = 0.1 → half-life ln2/0.1 ≈ 6.93
    spread = []
    val = 10.0
    # deterministic: mean-reverting towards 0 with slope -0.1 (kappa=0.1)
    for _ in range(500):
        val = val - 0.1 * val  # dx = -kappa*x, no noise
        spread.append(val)
    hl = half_life_ou(spread)
    assert math.isfinite(hl)
    assert 5.0 < hl < 10.0


def test_half_life_ou_non_mean_reverting():
    # Constant series → zero variance in (s_{t-1}-μ) → inf (no mean reversion signal).
    hl = half_life_ou([5.0] * 50)
    assert math.isinf(hl)


def test_half_life_too_short():
    with pytest.raises(ValueError):
        half_life_ou([1.0, 2.0])


def test_cointegration_analysis_cointegrated():
    # Build x as a smooth trending series and y = 2x + 1 + stationary spread.
    # Use a full number of sin periods so the spread is ~orthogonal to x's trend.
    n = 300
    x = [100.0 + 0.5 * i for i in range(n)]
    # full periods: sin(2π·k·i/n) over [0,n) sums to ~0 → orthogonal to linear trend
    k = 6
    spread_truth = [0.5 * math.sin(2 * math.pi * k * i / n) for i in range(n)]
    y = [2.0 * xi + 1.0 + s for xi, s in zip(x, spread_truth)]
    res = cointegration_analysis(y, x)
    assert abs(res.beta - 2.0) < 1e-2
    # alpha absorbs any nonzero mean of the spread; check beta + R² instead.
    assert abs(res.alpha - 1.0) < 0.2
    assert res.r_squared > 0.99
    assert math.isfinite(res.half_life)


def test_cointegration_analysis_to_dict_keys():
    res = cointegration_analysis([1.0, 2.0, 3.0, 4.0, 5.0], [0.5, 1.0, 1.5, 2.0, 2.5])
    d = res.to_dict()
    assert "half_life_finite" in d
    assert "current_zscore" in d
    assert "beta" in d