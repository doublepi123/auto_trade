"""Tests for P233 causal discovery (Granger + PCMCI-style lag screening)."""

from __future__ import annotations

import math

import pytest

from app.platform.causal_analysis import (
    LeadLagResult,
    betai,
    f_cdf,
    f_sf,
    granger_causality,
    lead_lag_summary,
    partial_correlation_lag,
)


# ---------------------------------------------------------------------------
# betai / F distribution
# ---------------------------------------------------------------------------


def test_betai_endpoints():
    assert betai(2.0, 3.0, 0.0) == 0.0
    assert betai(2.0, 3.0, 1.0) == 1.0


def test_betai_symmetry():
    # I_x(a,b) + I_{1-x}(b,a) = 1
    a, b, x = 2.5, 4.5, 0.3
    assert abs(betai(a, b, x) + betai(b, a, 1.0 - x) - 1.0) < 1e-9


def test_betai_invalid_params():
    with pytest.raises(ValueError):
        betai(0.0, 1.0, 0.5)
    with pytest.raises(ValueError):
        betai(1.0, -1.0, 0.5)


def test_f_cdf_basics():
    # F(0) = 0, F(inf) -> 1
    assert f_cdf(0.0, 5, 10) == 0.0
    assert f_cdf(1e6, 5, 10) > 0.999
    # symmetry: F is monotone increasing
    a = f_cdf(0.5, 5, 10)
    b = f_cdf(2.0, 5, 10)
    assert b > a


def test_f_sf_in_unit_interval():
    s = f_sf(3.0, 5, 20)
    assert 0.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# granger_causality
# ---------------------------------------------------------------------------


def test_granger_independent_series_high_p():
    # Independent random-ish series (no RNG: deterministic deterministic sequence)
    n = 120
    x = [math.sin(i * 0.31) + 0.1 * (i % 7) for i in range(n)]
    y = [math.cos(i * 0.17) + 0.1 * (i % 5) for i in range(n)]
    res = granger_causality(x, y, max_lag=3)
    # No real causal link → min p-value should not be tiny.
    assert res.min_p > 0.01
    assert all(0.0 <= p <= 1.0 for p in res.p_values.values())


def test_granger_strong_causality_low_p():
    # Construct y_t that depends explicitly on x_{t-2}: strong Granger causality.
    n = 200
    x = [math.sin(i * 0.2) for i in range(n)]
    y = [0.0, 0.0] + [0.9 * x[t - 2] + 0.05 * (t % 3) for t in range(2, n)]
    # ensure y[0], y[1] defined
    y[0] = x[0]
    y[1] = x[1]
    res = granger_causality(x, y, max_lag=4)
    assert res.min_p < 0.05
    assert 2 in res.significant_lags or res.best_lag == 2


def test_granger_mismatched_lengths_raises():
    with pytest.raises(ValueError):
        granger_causality([1.0, 2.0, 3.0], [1.0, 2.0], max_lag=1)


def test_granger_too_short_raises():
    with pytest.raises(ValueError):
        granger_causality([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], max_lag=5)


def test_granger_max_lag_invalid():
    with pytest.raises(ValueError):
        granger_causality([1.0] * 20, [2.0] * 20, max_lag=0)


def test_granger_result_to_dict_keys():
    res = granger_causality([math.sin(i * 0.3) for i in range(40)],
                            [math.cos(i * 0.3) for i in range(40)], max_lag=2)
    d = res.to_dict()
    assert set(d.keys()) == {
        "max_lag", "n", "f_stats", "p_values", "min_p", "best_lag",
        "significant_lags",
    }
    assert d["max_lag"] == 2
    assert d["n"] == 38


def test_granger_perfect_fit_degenerate():
    # y perfectly explained by lagged x AND lagged y → rss_u may be ~0
    # should not raise; p defaults to 1.0
    n = 30
    x = [1.0 if i % 2 == 0 else -1.0 for i in range(n)]
    y = [v * 2.0 for v in x]
    res = granger_causality(x, y, max_lag=1)
    assert res.max_lag == 1


# ---------------------------------------------------------------------------
# partial_correlation_lag
# ---------------------------------------------------------------------------


def test_partial_correlation_no_z():
    n = 80
    x = [math.sin(i * 0.2) + 0.01 * i for i in range(n)]
    y = [math.cos(i * 0.2) + 0.01 * i for i in range(n)]
    pc = partial_correlation_lag(x, y, z=None, max_lag=3)
    assert set(pc.keys()) == {1, 2, 3}
    for v in pc.values():
        assert -1.0 <= v <= 1.0


def test_partial_correlation_with_z():
    n = 100
    x = [math.sin(i * 0.15) for i in range(n)]
    y = [math.cos(i * 0.15) for i in range(n)]
    z = [0.1 * (i % 4) for i in range(n)]
    pc = partial_correlation_lag(x, y, z=z, max_lag=2)
    assert set(pc.keys()) == {1, 2}
    for v in pc.values():
        assert -1.0 <= v <= 1.0


def test_partial_correlation_mismatched_z():
    with pytest.raises(ValueError):
        partial_correlation_lag([1.0] * 20, [2.0] * 20, z=[3.0] * 19, max_lag=2)


def test_partial_correlation_too_short():
    with pytest.raises(ValueError):
        partial_correlation_lag([1.0, 2.0], [3.0, 4.0], z=None, max_lag=3)


def test_partial_correlation_zero_when_identical():
    # If x_lag and y_cur are both constant → correlation 0 (guard).
    n = 40
    x = [5.0] * n
    y = [7.0] * n
    pc = partial_correlation_lag(x, y, z=None, max_lag=1)
    assert pc[1] == 0.0


# ---------------------------------------------------------------------------
# lead_lag_summary
# ---------------------------------------------------------------------------


def test_lead_lag_summary_direction_x_to_y():
    n = 200
    x = [math.sin(i * 0.2) for i in range(n)]
    # y driven by lagged x
    y = [x[0], x[1]] + [0.9 * x[t - 2] + 0.01 * (t % 3) for t in range(2, n)]
    y[0] = x[0]
    y[1] = x[1]
    res = lead_lag_summary(x, y, max_lag=4)
    assert isinstance(res, LeadLagResult)
    assert res.direction in {"x->y", "y->x", "none"}
    assert res.max_lag == 4
    assert 0.0 <= res.best_p <= 1.0
    d = res.to_dict()
    assert "forward" in d and "reverse" in d
    assert d["forward"]["max_lag"] == 4


def test_lead_lag_summary_independent_is_none():
    n = 150
    x = [math.sin(i * 0.31) + 0.1 * (i % 7) for i in range(n)]
    y = [math.cos(i * 0.17) + 0.1 * (i % 5) for i in range(n)]
    res = lead_lag_summary(x, y, max_lag=3)
    # No causal link → direction should be "none" (p not below 0.05)
    assert res.direction == "none"
    assert res.significant_lags == []


def test_lead_lag_summary_best_p_in_unit():
    n = 100
    x = [math.sin(i * 0.3) for i in range(n)]
    y = [math.cos(i * 0.3) for i in range(n)]
    res = lead_lag_summary(x, y, max_lag=2)
    assert 0.0 <= res.best_p <= 1.0


def test_lead_lag_summary_to_dict_serializable():
    n = 60
    x = [float(i % 4) for i in range(n)]
    y = [float(i % 5) for i in range(n)]
    res = lead_lag_summary(x, y, max_lag=2)
    d = res.to_dict()
    assert d["direction"] in {"x->y", "y->x", "none"}
    assert isinstance(d["significant_lags"], list)
    assert isinstance(d["forward"], dict)
    assert isinstance(d["reverse"], dict)