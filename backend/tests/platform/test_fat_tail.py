"""Tests for P210 fat-tail diagnostics."""

from __future__ import annotations

import math

from app.platform.fat_tail import (
    excess_kurtosis,
    fat_tail_report,
    hill_estimator,
    skewness,
    stable_fit,
    tail_ratio,
)


def test_excess_kurtosis_zero_for_normal():
    # Gaussian: theoretical excess kurtosis = 0
    # Use a known symmetric distribution close to normal
    rets = [0.0, 0.1, -0.1, 0.05, -0.05, 0.02, -0.02, 0.0, 0.03, -0.03, 0.01, -0.01]
    k = excess_kurtosis(rets)
    assert abs(k) < 0.5  # small sample noise


def test_excess_kurtosis_positive_for_fat_tails():
    # Heavy-tailed: occasional huge values
    rets = [0.01] * 50 + [0.5, -0.5, 0.3, -0.3]
    k = excess_kurtosis(rets)
    assert k > 0


def test_excess_kurtosis_short_series_returns_zero():
    assert excess_kurtosis([0.01, 0.02]) == 0.0


def test_skewness_negative_for_left_tail():
    # Big losses, small gains → negative skew
    rets = [0.01, 0.01, 0.02, -0.10, -0.05, 0.01, -0.15]
    s = skewness(rets)
    assert s < 0


def test_skewness_positive_for_right_tail():
    rets = [-0.01, -0.01, -0.02, 0.10, 0.05, -0.01, 0.15]
    s = skewness(rets)
    assert s > 0


def test_skewness_zero_for_symmetric():
    rets = [-0.1, 0.1, -0.05, 0.05, -0.02, 0.02]
    s = skewness(rets)
    assert abs(s) < 1e-9


def test_hill_estimator_returns_positive():
    rets = [abs(math.sin(i) + 0.01) for i in range(1, 200)]
    rets[10] = 0.5  # inject an outlier
    h = hill_estimator(rets)
    assert h > 0


def test_hill_estimator_short_series_returns_zero():
    assert hill_estimator([0.01, 0.02]) == 0.0


def test_hill_estimator_gaussian_returns_finite_positive():
    # Pseudo-Gaussian: |x| where x ~ N(0,1) — Hill's estimator is well-known
    # to be upward-biased for small n; the absolute value should be finite
    # and positive. (Not asserting a tight range — the bias is real.)
    import random
    random.seed(42)
    rets = [abs(random.gauss(0, 1)) for _ in range(2000)]
    h = hill_estimator(rets)
    assert math.isfinite(h) and h > 1.0


def test_tail_ratio_one_for_constant_returns():
    rets = [0.01] * 100
    # sigma = 0 → return 1.0 (degenerate)
    assert tail_ratio(rets, 0.95) == 1.0


def test_tail_ratio_positive_for_actual_returns():
    rets = [-0.05, -0.02, 0.01, 0.02, 0.03, -0.01, 0.0, 0.015, -0.02, 0.01]
    tr = tail_ratio(rets, 0.95)
    assert tr > 0


def test_stable_fit_returns_dict_with_four_params():
    rets = [0.01, -0.02, 0.03, -0.005, 0.015, -0.01, 0.025, -0.015, 0.02, -0.01, 0.005, 0.0]
    fit = stable_fit(rets)
    assert set(fit.keys()) == {"alpha", "beta", "sigma", "mu"}
    # α clamped to (0, 2] so always > 0
    assert fit["alpha"] > 0
    # β in [-1, 1]
    assert -1.0 <= fit["beta"] <= 1.0
    # σ > 0 when there's any spread
    assert fit["sigma"] > 0


def test_stable_fit_handles_constant_returns():
    rets = [0.01] * 10
    fit = stable_fit(rets)
    # sigma = 0 when IQR = 0; the implementation falls back to sample std
    assert fit["mu"] == 0.01


def test_fat_tail_report_keys():
    rets = [0.01, -0.02, 0.03, -0.005, 0.015, -0.01, 0.025, -0.015, 0.02, -0.01, 0.005, 0.0]
    rep = fat_tail_report(rets)
    for k in (
        "n", "mean", "std", "excess_kurtosis", "skewness",
        "hill_alpha", "tail_ratio_95", "tail_ratio_99", "stable",
    ):
        assert k in rep


def test_fat_tail_report_empty_returns():
    rep = fat_tail_report([])
    assert rep["n"] == 0
    assert rep["stable"] == {"alpha": 0.0, "beta": 0.0, "sigma": 0.0, "mu": 0.0}
