"""Tests for P256 fixed-income analytics."""

from __future__ import annotations

import math

import pytest

from app.platform.fixed_income import (
    bond_analytics,
    bond_price,
    convexity,
    forward_rate,
    macaulay_duration,
    modified_duration,
    yield_to_maturity,
)


def test_zero_coupon_bond_price():
    # Zero-coupon bond: P = F / (1+y)^n
    p = bond_price(0.05, 100.0, 0.0, 10)
    assert abs(p - 100.0 / 1.05 ** 10) < 1e-9


def test_par_bond_ytm_equals_coupon_rate():
    # Bond priced at par with coupon = face * ytm.
    ytm = 0.05
    p = bond_price(ytm, 100.0, 5.0, 10)
    assert abs(p - 100.0) < 1e-9
    # Inverting: YTM should equal the coupon rate.
    inv = yield_to_maturity(100.0, 100.0, 5.0, 10)
    assert abs(inv - ytm) < 1e-8


def test_ytm_inverse_of_price():
    for ytm in [0.02, 0.05, 0.08, 0.10]:
        p = bond_price(ytm, 1000.0, 40.0, 20)
        inv = yield_to_maturity(p, 1000.0, 40.0, 20)
        assert abs(inv - ytm) < 1e-9


def test_ytm_deep_discount():
    # Low-coupon long bond trades at a deep discount -> high YTM.
    p = bond_price(0.0, 100.0, 0.0, 5)  # zero-coupon at ytm=0 -> price=100
    # price < 100 implies positive ytm.
    inv = yield_to_maturity(80.0, 100.0, 0.0, 5)
    assert inv > 0.0
    # Check: 80 = 100/(1+y)^5 -> y = (100/80)^(1/5)-1
    expected = (100.0 / 80.0) ** (1.0 / 5) - 1.0
    assert abs(inv - expected) < 1e-9


def test_macaulay_duration_zero_coupon_equals_maturity():
    # Macaulay duration of a zero-coupon bond = its maturity (periods).
    d = macaulay_duration(0.05, 100.0, 0.0, 10)
    assert abs(d - 10.0) < 1e-9


def test_macaulay_duration_coupon_bond_less_than_maturity():
    d = macaulay_duration(0.05, 100.0, 5.0, 10)
    assert 0.0 < d < 10.0


def test_modified_duration_relation():
    ytm = 0.06
    mac = macaulay_duration(ytm, 100.0, 5.0, 10)
    mod = modified_duration(ytm, 100.0, 5.0, 10)
    assert abs(mod - mac / (1.0 + ytm)) < 1e-12


def test_convexity_positive():
    c = convexity(0.05, 100.0, 5.0, 10)
    assert c > 0.0


def test_convexity_zero_coupon_closed_form():
    # Zero-coupon convexity = n(n+1)/(1+y)^2 (the price F/(1+y)^n cancels one power).
    ytm, n = 0.05, 10
    c = convexity(ytm, 100.0, 0.0, n)
    expected = n * (n + 1) / (1.0 + ytm) ** 2
    assert abs(c - expected) < 1e-6


def test_forward_rate_no_arbitrage():
    # Forward rate consistent with two spot rates.
    f = forward_rate(0.03, 0.04, 1.0, 2.0)
    # (1.04)^2 = (1.03)(1+f) -> f = 1.04^2/1.03 - 1
    expected = 1.04 ** 2 / 1.03 - 1.0
    assert abs(f - expected) < 1e-9


def test_forward_rate_exceeds_spot_when_curve_steepening():
    f = forward_rate(0.02, 0.05, 1.0, 2.0)
    assert f > 0.05


def test_bond_analytics_aggregate():
    res = bond_analytics(95.0, 100.0, 4.0, 10)
    d = res.to_dict()
    assert d["price"] == 95.0
    assert d["ytm"] > 0.04  # discount bond -> ytm > coupon rate
    assert d["macaulay_duration"] > 0.0
    assert d["convexity"] > 0.0


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        bond_price(0.05, 100.0, 5.0, 0)
    with pytest.raises(ValueError):
        yield_to_maturity(-1.0, 100.0, 5.0, 10)
    with pytest.raises(ValueError):
        forward_rate(0.03, 0.04, 2.0, 1.0)


def test_to_dict_roundtrip():
    res = bond_analytics(100.0, 100.0, 5.0, 10)
    d = res.to_dict()
    for k in ("price", "ytm", "macaulay_duration", "modified_duration", "convexity"):
        assert k in d