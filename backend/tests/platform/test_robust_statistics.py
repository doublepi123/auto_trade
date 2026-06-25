"""Tests for P248 robust statistics."""

from __future__ import annotations

import statistics

import pytest

from app.platform.robust_statistics import (
    huber,
    mad,
    robust_stats,
    theil_sen,
    trimmed_mean,
    winsorize,
)


def test_mad_known_value():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    # median 3; abs devs [2,1,0,1,2] -> median 1; normalized 1.4826
    assert abs(mad(xs, normalize=False) - 1.0) < 1e-12
    assert abs(mad(xs, normalize=True) - 1.482602218505602) < 1e-9


def test_mad_ignores_outlier_scale():
    clean = [1.0, 2.0, 3.0, 4.0, 5.0]
    with_outlier = [1.0, 2.0, 3.0, 4.0, 5.0, 1000.0]
    # MAD should barely move vs std which explodes.
    assert abs(mad(clean) - mad(with_outlier)) < 1.0
    assert statistics.stdev(with_outlier) > 10 * statistics.stdev(clean)


def test_winsorize_clips_extremes():
    xs = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 100.0]
    w = winsorize(xs, alpha=0.1)
    assert max(w) < 100.0
    assert min(w) >= 0.0


def test_winsorize_invalid_alpha():
    with pytest.raises(ValueError):
        winsorize([1.0, 2.0], alpha=0.6)


def test_trimmed_mean_drops_extremes():
    xs = [1.0, 2.0, 3.0, 4.0, 100.0]
    # 20% trim drops 1 value each end -> mean of [2,3,4] = 3
    assert abs(trimmed_mean(xs, alpha=0.2) - 3.0) < 1e-12


def test_trimmed_mean_full_sample_when_alpha_zero():
    xs = [1.0, 2.0, 3.0, 4.0]
    assert abs(trimmed_mean(xs, alpha=0.0) - statistics.mean(xs)) < 1e-12


def test_theil_sen_recovers_linear_slope():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [2.0 + 3.0 * xi for xi in x]
    slope, intercept = theil_sen(y, x)
    assert abs(slope - 3.0) < 1e-9
    assert abs(intercept - 2.0) < 1e-9


def test_theil_sen_robust_to_outlier():
    x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    y = [2.0 + 3.0 * xi for xi in x]
    y[3] = 1000.0  # one gross outlier
    slope, _ = theil_sen(y, x)
    # OLS would be dragged far off; Theil-Sen stays near 3.
    assert abs(slope - 3.0) < 0.5


def test_theil_sen_length_mismatch_raises():
    with pytest.raises(ValueError):
        theil_sen([1.0, 2.0], [1.0])


def test_theil_sen_identical_x_raises():
    with pytest.raises(ValueError):
        theil_sen([1.0, 2.0, 3.0], [5.0, 5.0, 5.0])


def test_huber_recovers_mean_clean():
    xs = [1.0, 1.1, 0.9, 1.05, 0.95]
    h = huber(xs)
    assert abs(h - statistics.mean(xs)) < 0.1


def test_huber_robust_to_outlier():
    clean = [1.0, 1.1, 0.9, 1.05, 0.95]
    with_outlier = [1.0, 1.1, 0.9, 1.05, 0.95, 50.0]
    # Huber stays near 1; OLS mean would be ~9.
    assert abs(huber(with_outlier) - 1.0) < 0.3


def test_huber_empty_raises():
    with pytest.raises(ValueError):
        huber([])


def test_robust_stats_aggregate():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    r = robust_stats(xs, y=[2.0 + 3.0 * v for v in xs], x=xs)
    d = r.to_dict()
    assert abs(d["median"] - 3.0) < 1e-9
    assert d["theil_sen_slope"] == 3.0
    assert "huber_location" in d


def test_robust_stats_no_regression():
    r = robust_stats([1.0, 2.0, 3.0])
    assert r.theil_sen_slope is None
    assert r.theil_sen_intercept is None


def test_robust_stats_empty_raises():
    with pytest.raises(ValueError):
        robust_stats([])