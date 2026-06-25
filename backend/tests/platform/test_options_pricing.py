"""Tests for P243 European option pricing + Greeks."""

from __future__ import annotations

import math

import pytest

from app.platform.options_pricing import (
    black_scholes,
    greeks,
    option_price,
)


def test_atm_call_price_matches_closed_form():
    # S=K=100, T=1, r=0.05, sigma=0.2, q=0 -> call ≈ 10.4506
    price = black_scholes("call", 100.0, 100.0, 1.0, 0.05, 0.20, 0.0)
    assert abs(price - 10.450583572185565) < 1e-9


def test_put_price_matches_closed_form():
    price = black_scholes("put", 100.0, 100.0, 1.0, 0.05, 0.20, 0.0)
    assert abs(price - 5.573526023256497) < 1e-9


def test_put_call_parity_no_dividend():
    S, K, T, r, sigma = 100.0, 105.0, 0.75, 0.04, 0.25
    c = black_scholes("call", S, K, T, r, sigma)
    p = black_scholes("put", S, K, T, r, sigma)
    # call - put = S - K e^{-rT}
    assert abs((c - p) - (S - K * math.exp(-r * T))) < 1e-9


def test_put_call_parity_with_dividend():
    S, K, T, r, sigma, q = 100.0, 100.0, 1.0, 0.05, 0.2, 0.02
    c = black_scholes("call", S, K, T, r, sigma, q)
    p = black_scholes("put", S, K, T, r, sigma, q)
    assert abs((c - p) - (S * math.exp(-q * T) - K * math.exp(-r * T))) < 1e-9


def test_call_delta_in_zero_one():
    g = greeks("call", 100.0, 100.0, 1.0, 0.05, 0.2)
    assert 0.0 < g["delta"] < 1.0
    # ATM-forward call delta near N(d1) ~ 0.6
    assert 0.5 < g["delta"] < 0.7


def test_put_delta_negative():
    g = greeks("put", 100.0, 100.0, 1.0, 0.05, 0.2)
    assert -1.0 < g["delta"] < 0.0


def test_gamma_vega_type_independent():
    gc = greeks("call", 100.0, 100.0, 1.0, 0.05, 0.2)
    gp = greeks("put", 100.0, 100.0, 1.0, 0.05, 0.2)
    assert abs(gc["gamma"] - gp["gamma"]) < 1e-12
    assert abs(gc["vega"] - gp["vega"]) < 1e-12


def test_vega_positive_and_sensible():
    g = greeks("call", 100.0, 100.0, 1.0, 0.05, 0.2)
    assert g["vega"] > 0.0
    # ATM vega ~ 0.5 * S * sqrt(T) ~ 39.9
    assert 35.0 < g["vega"] < 45.0


def test_call_rho_positive_put_rho_negative():
    gc = greeks("call", 100.0, 100.0, 1.0, 0.05, 0.2)
    gp = greeks("put", 100.0, 100.0, 1.0, 0.05, 0.2)
    assert gc["rho"] > 0.0
    assert gp["rho"] < 0.0


def test_theta_negative_for_atm_call():
    g = greeks("call", 100.0, 100.0, 1.0, 0.05, 0.2)
    # ATM call theta is negative (time decay)
    assert g["theta"] < 0.0


def test_call_delta_finite_diff():
    # Verify delta by central finite difference.
    S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.2
    h = 1e-4
    up = black_scholes("call", S + h, K, T, r, sigma)
    dn = black_scholes("call", S - h, K, T, r, sigma)
    fd = (up - dn) / (2 * h)
    g = greeks("call", S, K, T, r, sigma)
    assert abs(fd - g["delta"]) < 1e-4


def test_vega_finite_diff():
    S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.2
    h = 1e-4
    up = black_scholes("call", S, K, T, r, sigma + h)
    dn = black_scholes("call", S, K, T, r, sigma - h)
    fd = (up - dn) / (2 * h)
    g = greeks("call", S, K, T, r, sigma)
    assert abs(fd - g["vega"]) < 1e-3


def test_invalid_option_type_raises():
    with pytest.raises(ValueError):
        black_scholes("straddle", 100.0, 100.0, 1.0, 0.05, 0.2)


def test_nonpositive_inputs_raise():
    with pytest.raises(ValueError):
        black_scholes("call", 100.0, 100.0, 0.0, 0.05, 0.2)
    with pytest.raises(ValueError):
        black_scholes("call", 100.0, 100.0, 1.0, 0.05, 0.0)
    with pytest.raises(ValueError):
        black_scholes("call", -1.0, 100.0, 1.0, 0.05, 0.2)


def test_option_result_aggregates():
    res = option_price("call", 100.0, 100.0, 1.0, 0.05, 0.2, 0.0)
    d = res.to_dict()
    assert d["option_type"] == "call"
    assert abs(d["price"] - 10.450583572185565) < 1e-9
    assert "delta" in d and "vanna" in d and "volga" in d