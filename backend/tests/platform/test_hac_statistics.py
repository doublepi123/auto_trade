"""P330: HAC (Newey-West) statistics tests."""

from __future__ import annotations

import pytest


def test_hac_slope_approx_2_and_t_stat_gt_2():
    """y = 2*x + noise → slope ≈ 2 with hac t-stat > 2."""
    from app.platform.hac_statistics import hac_statistics_report

    import math
    import random

    rng = random.Random(42)
    n = 100
    x = [rng.uniform(-1, 1) for _ in range(n)]
    noise = [rng.gauss(0, 0.1) for _ in range(n)]
    y = [2.0 * xi + ni for xi, ni in zip(x, noise)]

    result = hac_statistics_report(y, [x], lags=5).to_dict()
    coef = result["coefficients"]
    hac_t = result["hac_t_stats"]
    # coef[0] is intercept, coef[1] is slope
    assert "const" in coef
    assert abs(coef["const"]) < 1.0  # near zero
    assert math.isclose(coef["x_0"], 2.0, abs_tol=0.5)
    assert "const" in hac_t
    assert hac_t["x_0"] > 2.0


def test_hac_rejects_short_series():
    from app.platform.hac_statistics import hac_statistics_report

    with pytest.raises(ValueError, match="at least 2"):
        hac_statistics_report([1.0], [[1.0]], lags=3)


def test_hac_rejects_length_mismatch():
    from app.platform.hac_statistics import hac_statistics_report

    with pytest.raises(ValueError, match="same length"):
        hac_statistics_report([1.0, 2.0, 3.0], [[1.0, 2.0, 3.0, 4.0]], lags=3)


def test_hac_rejects_non_finite():
    from app.platform.hac_statistics import hac_statistics_report

    with pytest.raises(ValueError, match="finite"):
        hac_statistics_report([1.0, float("nan")], [[1.0, 2.0]], lags=3)


def test_hac_rejects_lags_ge_n():
    from app.platform.hac_statistics import hac_statistics_report
    with pytest.raises(ValueError):
        hac_statistics_report([1, 2, 3], [[2, 3, 4]], lags=10)


def test_hac_rejects_n_le_k():
    from app.platform.hac_statistics import hac_statistics_report
    with pytest.raises(ValueError):
        hac_statistics_report([1, 2], [[3, 4], [5, 6]], lags=1)
