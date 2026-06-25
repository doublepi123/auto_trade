"""Tests for P244 implied volatility + SVI."""

from __future__ import annotations

import math

import pytest

from app.platform.implied_volatility import (
    implied_volatility,
    svi_fit,
    svi_total_variance,
)
from app.platform.options_pricing import black_scholes


def test_implied_vol_roundtrip_call():
    # Price an ATM call at sigma=0.2, invert, recover sigma.
    S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.2
    price = black_scholes("call", S, K, T, r, sigma)
    iv = implied_volatility("call", price, S, K, T, r)
    assert abs(iv - sigma) < 1e-8


def test_implied_vol_roundtrip_put():
    S, K, T, r, q, sigma = 100.0, 110.0, 0.5, 0.03, 0.02, 0.35
    price = black_scholes("put", S, K, T, r, sigma, q)
    iv = implied_volatility("put", price, S, K, T, r, q)
    assert abs(iv - sigma) < 1e-8


def test_implied_vol_itm_otm_range():
    for K in [80.0, 90.0, 100.0, 110.0, 120.0]:
        price = black_scholes("call", 100.0, K, 1.0, 0.05, 0.25)
        iv = implied_volatility("call", price, 100.0, K, 1.0, 0.05)
        assert abs(iv - 0.25) < 1e-7


def test_implied_vol_outside_bounds_raises():
    # price below intrinsic -> no-arb violation
    with pytest.raises(ValueError):
        implied_volatility("call", 0.01, 100.0, 100.0, 1.0, 0.05)
    # price above asset-forward
    with pytest.raises(ValueError):
        implied_volatility("call", 200.0, 100.0, 100.0, 1.0, 0.05)


def test_implied_vol_nonpositive_inputs_raise():
    with pytest.raises(ValueError):
        implied_volatility("call", 5.0, 100.0, 100.0, 0.0, 0.05)
    with pytest.raises(ValueError):
        implied_volatility("call", -1.0, 100.0, 100.0, 1.0, 0.05)


def test_svi_total_variance_symmetric_atm():
    # rho=0, m=0 -> symmetric; w(0) = a + b*sigma
    a, b, rho, m, sigma = 0.04, 0.3, 0.0, 0.0, 0.1
    w0 = svi_total_variance(0.0, a, b, rho, m, sigma)
    assert abs(w0 - (a + b * sigma)) < 1e-12
    # symmetry w(k) == w(-k)
    wk = svi_total_variance(0.2, a, b, rho, m, sigma)
    w_neg = svi_total_variance(-0.2, a, b, rho, m, sigma)
    assert abs(wk - w_neg) < 1e-12


def test_svi_total_variance_asymmetry_with_rho():
    a, b, rho, m, sigma = 0.04, 0.3, -0.5, 0.0, 0.1
    wk = svi_total_variance(0.3, a, b, rho, m, sigma)
    w_neg = svi_total_variance(-0.3, a, b, rho, m, sigma)
    # negative rho -> right wing lower than left
    assert wk < w_neg


def test_svi_fit_recovers_params_from_clean_data():
    # Generate a clean SVI slice and recover params.
    a, b, rho, m, sigma = 0.04, 0.4, -0.3, 0.1, 0.1
    T = 1.0
    ks = [k * 0.1 for k in range(-20, 21)]  # -2.0 .. 2.0
    ivs = [math.sqrt(max(svi_total_variance(k, a, b, rho, m, sigma) / T, 1e-12)) for k in ks]
    fit = svi_fit(ks, ivs, T)
    assert abs(fit.a - a) < 1e-3
    assert abs(fit.b - b) < 1e-3
    assert abs(fit.rho - rho) < 1e-2
    assert abs(fit.m - m) < 1e-3
    assert abs(fit.sigma - sigma) < 1e-3
    assert fit.rms < 1e-8


def test_svi_fit_admissibility_constraints():
    # Fit on noisy-ish symmetric data; params must respect bounds.
    a, b, rho, m, sigma = 0.02, 0.2, 0.0, 0.0, 0.05
    T = 0.5
    ks = [k * 0.15 for k in range(-10, 11)]
    ivs = [math.sqrt(max(svi_total_variance(k, a, b, rho, m, sigma) / T, 1e-12)) for k in ks]
    fit = svi_fit(ks, ivs, T)
    assert fit.a >= 0.0
    assert fit.b >= 0.0
    assert -1.0 <= fit.rho <= 1.0
    assert fit.sigma > 0.0


def test_svi_fit_too_few_points_raises():
    with pytest.raises(ValueError):
        svi_fit([0.0, 0.1, 0.2], [0.2, 0.21, 0.22], 1.0)


def test_svi_fit_length_mismatch_raises():
    with pytest.raises(ValueError):
        svi_fit([0.0, 0.1, 0.2, 0.3, 0.4], [0.2, 0.21, 0.22], 1.0)


def test_svi_fit_nonpositive_t_raises():
    with pytest.raises(ValueError):
        svi_fit([0.0, 0.1, 0.2, 0.3, 0.4], [0.2, 0.21, 0.22, 0.23, 0.24], 0.0)


def test_svi_fit_nonpositive_vol_raises():
    with pytest.raises(ValueError):
        svi_fit([0.0, 0.1, 0.2, 0.3, 0.4], [0.2, 0.0, 0.22, 0.23, 0.24], 1.0)