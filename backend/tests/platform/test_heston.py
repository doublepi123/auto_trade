"""Tests for P254 Heston model (characteristic function + moment-matched QMC)."""

from __future__ import annotations

import math

import pytest

from app.platform.heston import (
    heston_characteristic_function,
    heston_moments,
    heston_price,
    heston_quasi_monte_carlo,
)
from app.platform.options_pricing import black_scholes


def test_characteristic_function_at_zero_is_one():
    phi = heston_characteristic_function(0.0, 0.04, 2.0, 0.04, 0.3, -0.5, 0.05, 1.0)
    assert abs(phi - 1.0) < 1e-9


def test_characteristic_function_modulus_leq_one():
    for u in [0.5, 1.0, 5.0, 20.0, 50.0]:
        phi = heston_characteristic_function(u, 0.04, 2.0, 0.04, 0.3, -0.5, 0.05, 1.0)
        assert abs(phi) <= 1.0 + 1e-6


def test_heston_qmc_converges_to_bs_call():
    # Zero vol-of-vol + v0 = theta -> Heston ~ Black-Scholes.
    bs = black_scholes("call", 100.0, 100.0, 1.0, 0.05, 0.2)
    p = heston_price("call", 100.0, 100.0, 1.0, 0.05, 0.04, 2.0, 0.04, 1e-6, 0.0,
                     n_paths=20000, n_steps=64, seed=1)
    assert abs(p - bs) < 0.10


def test_heston_qmc_converges_to_bs_put():
    bs = black_scholes("put", 100.0, 100.0, 1.0, 0.05, 0.2)
    p = heston_price("put", 100.0, 100.0, 1.0, 0.05, 0.04, 2.0, 0.04, 1e-6, 0.0,
                     n_paths=20000, n_steps=64, seed=2)
    assert abs(p - bs) < 0.10


def test_heston_qmc_put_call_parity_approx():
    S, K, T, r = 100.0, 100.0, 1.0, 0.05
    c = heston_price("call", S, K, T, r, 0.04, 2.0, 0.04, 0.3, -0.5, seed=3)
    p = heston_price("put", S, K, T, r, 0.04, 2.0, 0.04, 0.3, -0.5, seed=4)
    assert abs((c - p) - (S - K * math.exp(-r * T))) < 0.20


def test_heston_call_itm_gt_otm():
    S, T, r = 100.0, 1.0, 0.05
    p_itm = heston_price("call", S, 80.0, T, r, 0.04, 2.0, 0.04, 0.3, -0.5, seed=5)
    p_otm = heston_price("call", S, 120.0, T, r, 0.04, 2.0, 0.04, 0.3, -0.5, seed=6)
    assert p_itm > p_otm > 0.0


def test_heston_smile_higher_theta_widens_otm_put():
    # Higher long-run variance theta unambiguously inflates OTM option prices
    # (more total variance). Robust to MC noise and independent of vol-of-vol.
    base = heston_price("put", 100.0, 110.0, 1.0, 0.05, 0.02, 2.0, 0.02, 0.3, -0.5,
                        n_paths=40000, seed=7)
    wide = heston_price("put", 100.0, 110.0, 1.0, 0.05, 0.08, 2.0, 0.08, 0.3, -0.5,
                        n_paths=40000, seed=8)
    assert wide > base


def test_heston_deterministic_with_seed():
    a = heston_quasi_monte_carlo("call", 100.0, 100.0, 1.0, 0.05, 0.04, 2.0, 0.04, 0.3, -0.5, seed=42)
    b = heston_quasi_monte_carlo("call", 100.0, 100.0, 1.0, 0.05, 0.04, 2.0, 0.04, 0.3, -0.5, seed=42)
    assert a.price == b.price
    assert a.standard_error > 0.0


def test_heston_moments_expected_spot():
    m = heston_moments(100.0, 0.04, 2.0, 0.04, 1.0, 0.05)
    assert abs(m["expected_spot"] - 100.0 * math.exp(0.05)) < 1e-9
    assert abs(m["expected_variance"] - 0.04) < 1e-9


def test_invalid_params_raise():
    with pytest.raises(ValueError):
        heston_characteristic_function(1.0, -0.01, 2.0, 0.04, 0.3, -0.5, 0.05, 1.0)
    with pytest.raises(ValueError):
        heston_characteristic_function(1.0, 0.04, 2.0, 0.04, 0.3, 1.5, 0.05, 1.0)
    with pytest.raises(ValueError):
        heston_quasi_monte_carlo("straddle", 100.0, 100.0, 1.0, 0.05, 0.04, 2.0, 0.04, 0.3, -0.5)
    with pytest.raises(ValueError):
        heston_quasi_monte_carlo("call", 100.0, 100.0, 0.0, 0.05, 0.04, 2.0, 0.04, 0.3, -0.5)


def test_to_dict_roundtrip():
    res = heston_quasi_monte_carlo("call", 100.0, 100.0, 1.0, 0.05, 0.04, 2.0, 0.04, 0.3, -0.5,
                                   n_paths=5000, seed=0)
    d = res.to_dict()
    assert d["option_type"] == "call"
    assert d["rho"] == -0.5
    assert d["n_paths"] == 5000
    assert "standard_error" in d