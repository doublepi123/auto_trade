"""Tests for P253 American options (CRR binomial tree)."""

from __future__ import annotations

import math

import pytest

from app.platform.american_options import (
    american_option_price,
    binomial_price,
    european_option_price,
)
from app.platform.options_pricing import black_scholes


def test_european_binomial_converges_to_bs():
    # European binomial with many steps should converge to BS closed form.
    S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.2
    bs = black_scholes("call", S, K, T, r, sigma)
    bin_eur = european_option_price("call", S, K, T, r, sigma, steps=500)
    assert abs(bin_eur - bs) < 0.05


def test_american_put_geq_european_put():
    # American put >= European put (early-exercise premium).
    S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.2
    am = american_option_price("put", S, K, T, r, sigma, steps=200)
    eur = european_option_price("put", S, K, T, r, sigma, steps=200)
    assert am >= eur - 1e-9


def test_american_call_no_dividend_equals_european():
    # With no dividends, American call == European call (never optimal to early-exercise).
    S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.2
    am = american_option_price("call", S, K, T, r, sigma, steps=200)
    eur = european_option_price("call", S, K, T, r, sigma, steps=200)
    assert abs(am - eur) < 0.01


def test_american_call_with_dividend_exceeds_european():
    # With dividends, early exercise of a call can be optimal -> American > European.
    S, K, T, r, sigma, q = 100.0, 100.0, 1.0, 0.05, 0.2, 0.10
    am = american_option_price("call", S, K, T, r, sigma, steps=300, dividend_yield=q)
    eur = european_option_price("call", S, K, T, r, sigma, steps=300, dividend_yield=q)
    assert am >= eur - 1e-9


def test_deep_itm_american_put_intrinsic_floor():
    # Deep ITM put with high r -> American put near intrinsic value (K - S).
    S, K, T, r, sigma = 50.0, 100.0, 1.0, 0.08, 0.3
    am = american_option_price("put", S, K, T, r, sigma, steps=300)
    intr = K - S
    assert am >= intr - 0.01  # never below intrinsic
    assert am - intr < 5.0  # but not far above for deep ITM + high r


def test_intrinsic_floor_at_expiry_for_call():
    # At expiry-like deep ITM, call ≈ intrinsic.
    S, K, T, r, sigma = 130.0, 100.0, 0.01, 0.05, 0.2
    am = american_option_price("call", S, K, T, r, sigma, steps=50)
    assert abs(am - 30.0) < 1.0


def test_early_exercise_nodes_counted():
    res = binomial_price("put", 80.0, 100.0, 1.0, 0.08, 0.3, steps=100, exercise="american")
    # Deep ITM American put should trigger at least one early-exercise node.
    assert res.early_exercise_nodes > 0
    d = res.to_dict()
    assert d["early_exercise_nodes"] == res.early_exercise_nodes


def test_convergence_increases_with_steps():
    S, K, T, r, sigma = 100.0, 100.0, 1.0, 0.05, 0.2
    p50 = european_option_price("call", S, K, T, r, sigma, steps=50)
    p500 = european_option_price("call", S, K, T, r, sigma, steps=500)
    bs = black_scholes("call", S, K, T, r, sigma)
    assert abs(p500 - bs) < abs(p50 - bs)


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        american_option_price("call", -1.0, 100.0, 1.0, 0.05, 0.2)
    with pytest.raises(ValueError):
        american_option_price("call", 100.0, 100.0, 0.0, 0.05, 0.2)
    with pytest.raises(ValueError):
        american_option_price("call", 100.0, 100.0, 1.0, 0.05, 0.0)
    with pytest.raises(ValueError):
        american_option_price("call", 100.0, 100.0, 1.0, 0.05, 0.2, steps=0)
    with pytest.raises(ValueError):
        binomial_price("straddle", 100.0, 100.0, 1.0, 0.05, 0.2)
    with pytest.raises(ValueError):
        binomial_price("call", 100.0, 100.0, 1.0, 0.05, 0.2, exercise="bermudan")


def test_to_dict_roundtrip():
    res = binomial_price("call", 100.0, 100.0, 1.0, 0.05, 0.2, steps=100)
    d = res.to_dict()
    assert d["option_type"] == "call"
    assert d["exercise"] == "american"
    assert 0.0 < d["risk_neutral_prob"] < 1.0
    assert d["up_factor"] > 1.0 > d["down_factor"]