"""Tests for P250 LOESS / LOWESS."""

from __future__ import annotations

import math

import pytest

from app.platform.loess import lowess


def test_lowess_recovers_linear():
    x = [float(i) for i in range(10)]
    y = [2.0 + 3.0 * xi for xi in x]
    res = lowess(x, y, bandwidth=0.5, iterations=0)
    for xv, yv, sv in zip(x, y, res.smoothed):
        assert abs(sv - yv) < 1e-6


def test_lowess_smooths_noise():
    x = [float(i) for i in range(20)]
    # Sinusoidal signal with small deterministic noise.
    y = [math.sin(xi / 3.0) + 0.05 * ((int(xi) * 7) % 5 - 2) * 0.1 for xi in x]
    res = lowess(x, y, bandwidth=0.3, iterations=2)
    assert len(res.smoothed) == 20
    # Smoothed series has lower variance than raw.
    def var(xs):
        m = sum(xs) / len(xs)
        return sum((v - m) ** 2 for v in xs) / len(xs)
    assert var(res.smoothed) <= var(y) + 1e-9


def test_lowess_robust_downweights_outlier():
    x = [float(i) for i in range(15)]
    y = [2.0 + 1.0 * xi for xi in x]
    y[7] = 100.0  # gross outlier
    res_no_robust = lowess(x, y, bandwidth=0.4, iterations=0)
    res_robust = lowess(x, y, bandwidth=0.4, iterations=4)
    # Robust fit should be much closer to the true line at the outlier.
    true_at_7 = 2.0 + 1.0 * 7.0
    assert abs(res_robust.smoothed[7] - true_at_7) < abs(res_no_robust.smoothed[7] - true_at_7)


def test_lowess_length_mismatch_raises():
    with pytest.raises(ValueError):
        lowess([1, 2, 3], [1, 2])


def test_lowess_empty_raises():
    with pytest.raises(ValueError):
        lowess([], [])


def test_lowess_too_few_points_raises():
    with pytest.raises(ValueError):
        lowess([1.0], [1.0])


def test_lowess_invalid_bandwidth_raises():
    with pytest.raises(ValueError):
        lowess([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], bandwidth=0.0)
    with pytest.raises(ValueError):
        lowess([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], bandwidth=1.5)


def test_lowess_to_dict_roundtrip():
    res = lowess([1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 6.0, 8.0], bandwidth=0.5)
    d = res.to_dict()
    assert d["n_points"] == 4
    assert len(d["smoothed"]) == 4


def test_lowess_preserves_input_order():
    x = [3.0, 1.0, 2.0, 5.0, 4.0]
    y = [6.0, 2.0, 4.0, 10.0, 8.0]
    res = lowess(x, y, bandwidth=0.6, iterations=0)
    # Smoothed aligned to input x order; for a perfect line recovered at each x.
    for xi, yi, si in zip(x, y, res.smoothed):
        assert abs(si - yi) < 1e-6