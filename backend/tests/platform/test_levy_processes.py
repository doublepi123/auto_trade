"""Tests for P349 Levy process option pricing (VG / CGMY)."""

from __future__ import annotations

import math

import pytest

from app.platform.levy_processes import (
    LevyProcessResult,
    levy_process_report,
)


def test_levy_process_report_vg_atm_call_positive():
    """ATM VG call option should have positive price."""
    result = levy_process_report(
        model="vg",
        spot=100.0,
        strike=100.0,
        expiry=1.0,
        sigma=0.2,
        nu=0.1,
        theta=-0.1,
        risk_free=0.02,
    )
    assert isinstance(result, LevyProcessResult)
    assert result.model == "vg"
    assert result.price > 0.0
    assert math.isfinite(result.price)
    assert math.isfinite(result.delta)
    assert isinstance(result.characteristic_function_eval, complex)


def test_levy_process_report_vg_itm_call_gt_otm():
    """ITM call price > OTM call price."""
    itm = levy_process_report(
        model="vg",
        spot=100.0,
        strike=80.0,
        expiry=1.0,
        sigma=0.2,
        nu=0.1,
        theta=-0.1,
        risk_free=0.02,
    )
    otm = levy_process_report(
        model="vg",
        spot=100.0,
        strike=120.0,
        expiry=1.0,
        sigma=0.2,
        nu=0.1,
        theta=-0.1,
        risk_free=0.02,
    )
    assert itm.price > otm.price > 0.0


def test_levy_process_report_vg_delta_in_0_1():
    """Call delta should be in (0, 1) for finite spot."""
    result = levy_process_report(
        model="vg",
        spot=100.0,
        strike=100.0,
        expiry=1.0,
        sigma=0.2,
        nu=0.1,
        theta=-0.1,
        risk_free=0.02,
    )
    assert 0.0 < result.delta < 1.0


def test_levy_process_report_vg_characteristic_at_u1():
    """Characteristic function at u=1 should be finite complex modulus <= 1."""
    result = levy_process_report(
        model="vg",
        spot=100.0,
        strike=100.0,
        expiry=1.0,
        sigma=0.2,
        nu=0.1,
        theta=-0.1,
        risk_free=0.02,
    )
    cf = result.characteristic_function_eval
    assert isinstance(cf, complex)
    assert abs(cf) <= 1.0 + 1e-9


def test_levy_process_report_rejects_cgmy_model():
    """CGMY model is not yet correctly implemented and should be rejected."""
    with pytest.raises(ValueError):
        levy_process_report(
            model="cgmy",
            spot=100.0,
            strike=100.0,
            expiry=1.0,
            sigma=0.15,
            nu=0.1,
            theta=-0.05,
            risk_free=0.02,
        )


def test_levy_process_report_invalid_model():
    with pytest.raises(ValueError):
        levy_process_report(
            model="invalid",
            spot=100.0,
            strike=100.0,
            expiry=1.0,
            sigma=0.2,
            nu=0.1,
            theta=-0.1,
        )


def test_levy_process_report_invalid_expiry():
    with pytest.raises(ValueError):
        levy_process_report(
            model="vg",
            spot=100.0,
            strike=100.0,
            expiry=-1.0,
            sigma=0.2,
            nu=0.1,
            theta=-0.1,
        )


def test_levy_process_report_invalid_sigma():
    with pytest.raises(ValueError):
        levy_process_report(
            model="vg",
            spot=100.0,
            strike=100.0,
            expiry=1.0,
            sigma=-0.2,
            nu=0.1,
            theta=-0.1,
        )


def test_levy_process_report_invalid_spot():
    with pytest.raises(ValueError):
        levy_process_report(
            model="vg",
            spot=-100.0,
            strike=100.0,
            expiry=1.0,
            sigma=0.2,
            nu=0.1,
            theta=-0.1,
        )


def test_levy_process_report_infinite_input():
    with pytest.raises(ValueError):
        levy_process_report(
            model="vg",
            spot=float("inf"),
            strike=100.0,
            expiry=1.0,
            sigma=0.2,
            nu=0.1,
            theta=-0.1,
        )


def test_levy_process_result_to_dict():
    result = levy_process_report(
        model="vg",
        spot=100.0,
        strike=100.0,
        expiry=1.0,
        sigma=0.2,
        nu=0.1,
        theta=-0.1,
    )
    d = result.to_dict()
    assert isinstance(d, dict)
    assert d["model"] == "vg"
    assert isinstance(d["price"], float)
    assert isinstance(d["delta"], float)
    assert isinstance(d["characteristic_function_eval"], complex)


def test_levy_process_rejects_extreme_params():
    import pytest
    from app.platform.levy_processes import levy_process_report
    with pytest.raises(ValueError):
        levy_process_report(model="vg", spot=100, strike=100, expiry=100, sigma=2.0, nu=0.01, theta=-5.0)
