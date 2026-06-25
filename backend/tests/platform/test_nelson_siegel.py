"""Tests for P255 Nelson-Siegel-Svensson yield curve."""

from __future__ import annotations

import math

import pytest

from app.platform.nelson_siegel import (
    discount_factor,
    fit_nss,
    nelson_siegel_rate,
    nelson_siegel_svensson_rate,
)


def test_nelson_siegel_rate_asymptotes_to_beta0():
    # As tau -> inf, the NS rate -> beta0 (loadings vanish).
    r = nelson_siegel_rate(10000.0, 0.04, -0.02, 0.01, 2.0)
    assert abs(r - 0.04) < 1e-5


def test_nelson_siegel_rate_at_zero_is_beta0_plus_beta1():
    r = nelson_siegel_rate(0.0, 0.04, -0.02, 0.01, 2.0)
    assert abs(r - 0.02) < 1e-9


def test_nss_rate_at_zero_is_beta0_plus_beta1():
    r = nelson_siegel_svensson_rate(0.0, 0.04, -0.02, 0.01, 0.005, 2.0, 7.0)
    assert abs(r - 0.02) < 1e-9


def test_nss_rate_asymptotes_to_beta0():
    r = nelson_siegel_svensson_rate(10000.0, 0.04, -0.02, 0.01, 0.005, 2.0, 7.0)
    assert abs(r - 0.04) < 1e-5


def test_discount_factor_between_zero_and_one():
    assert 0.0 < discount_factor(0.05, 10.0) < 1.0
    assert abs(discount_factor(0.0, 5.0) - 1.0) < 1e-12


def test_fit_nss_recovers_params_from_clean_curve():
    b0, b1, b2, b3, t1, t2 = 0.04, -0.02, 0.01, 0.005, 2.0, 7.0
    taus = [0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0]
    ys = [nelson_siegel_svensson_rate(t, b0, b1, b2, b3, t1, t2) for t in taus]
    fit = fit_nss(taus, ys)
    # The fit should reproduce the curve to high precision.
    assert fit.rms < 1e-7
    # Long-rate level recovered.
    assert abs(fit.beta0 - b0) < 1e-3


def test_fit_nss_recovers_pure_nelson_siegel():
    # beta3 = 0 -> NSS degenerates to NS; fit should reproduce the curve.
    b0, b1, b2, t1 = 0.03, -0.01, 0.02, 3.0
    taus = [0.5, 1.0, 2.0, 5.0, 10.0, 20.0]
    ys = [nelson_siegel_rate(t, b0, b1, b2, t1) for t in taus]
    fit = fit_nss(taus, ys)
    assert fit.rms < 1e-4


def test_fit_nss_admissibility_tau_constraints():
    taus = [0.5, 1.0, 2.0, 5.0, 10.0]
    ys = [0.02, 0.025, 0.03, 0.035, 0.04]
    fit = fit_nss(taus, ys)
    assert fit.tau1 > 0.0
    assert fit.tau2 > fit.tau1
    assert fit.rms >= 0.0


def test_fit_nss_invalid_inputs_raise():
    with pytest.raises(ValueError):
        fit_nss([1.0, 2.0], [0.03])
    with pytest.raises(ValueError):
        fit_nss([], [])
    with pytest.raises(ValueError):
        fit_nss([1.0], [0.03])
    with pytest.raises(ValueError):
        fit_nss([-1.0, 1.0], [0.03, 0.04])


def test_invalid_decay_raises():
    with pytest.raises(ValueError):
        nelson_siegel_rate(1.0, 0.04, -0.02, 0.01, 0.0)
    with pytest.raises(ValueError):
        nelson_siegel_svensson_rate(1.0, 0.04, -0.02, 0.01, 0.005, 0.0, 0.0)


def test_to_dict_roundtrip():
    taus = [0.5, 1.0, 2.0, 5.0, 10.0]
    ys = [0.02, 0.025, 0.03, 0.035, 0.04]
    d = fit_nss(taus, ys).to_dict()
    for k in ("beta0", "beta1", "beta2", "beta3", "tau1", "tau2", "rms"):
        assert k in d