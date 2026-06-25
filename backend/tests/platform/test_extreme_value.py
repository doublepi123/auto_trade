"""Tests for P232 extreme value theory (GPD tail fit)."""

from __future__ import annotations

import math

import pytest

from app.platform.extreme_value import (
    evt_cvar,
    evt_report,
    evt_var,
    gpd_fit,
    peaks_over_threshold,
)


def test_peaks_over_threshold_basic():
    losses = [0.1, 0.5, 1.0, 2.0, 0.3]
    exc = peaks_over_threshold(losses, threshold=0.4)
    assert len(exc) == 3
    assert abs(exc[0] - 0.1) < 1e-9
    assert abs(exc[1] - 0.6) < 1e-9
    assert abs(exc[2] - 1.6) < 1e-9


def test_peaks_over_threshold_empty():
    with pytest.raises(ValueError):
        peaks_over_threshold([], threshold=0.5)


def test_peaks_over_threshold_none_above():
    exc = peaks_over_threshold([0.1, 0.2, 0.3], threshold=0.5)
    assert exc == []


def test_gpd_fit_too_few():
    with pytest.raises(ValueError):
        gpd_fit([1.0])


def test_gpd_fit_constant_exceedances():
    # zero variance → xi=0, sigma=mean
    fit = gpd_fit([0.5, 0.5, 0.5])
    assert fit.xi == 0.0
    assert abs(fit.sigma - 0.5) < 1e-9


def test_gpd_fit_heavy_tail_positive_xi():
    # Very heavy-tailed exceedances (Pareto-like, fat tail) → positive xi.
    # Use a sequence where mean²/var is large: small typical values + occasional spikes.
    fit = gpd_fit([1.0, 1.0, 1.0, 1.0, 20.0, 30.0])
    assert fit.xi > 0  # heavy tail


def test_evt_var_increases_with_alpha():
    losses = [abs(0.01 * (i % 7 - 3)) + 0.02 * (i % 11 == 0) for i in range(200)]
    losses = [l * 10 for l in losses]  # scale up
    threshold = sorted(losses)[int(0.9 * len(losses))]
    v99 = evt_var(losses, threshold, 0.99)
    v999 = evt_var(losses, threshold, 0.999)
    assert v999 >= v99


def test_evt_var_alpha_range():
    losses = [0.1, 0.2, 0.3, 0.5, 1.0, 2.0]
    with pytest.raises(ValueError):
        evt_var(losses, 0.2, 1.5)


def test_evt_cvar_greater_than_var():
    losses = [0.1 * (i + 1) for i in range(50)]
    threshold = 2.0
    var = evt_var(losses, threshold, 0.95)
    cvar = evt_cvar(losses, threshold, 0.95)
    # CVaR is the average beyond VaR → should be ≥ VaR
    assert cvar >= var - 1e-9


def test_evt_report_keys():
    losses = [float(abs(i - 25)) * 0.1 for i in range(50)]
    threshold = 1.5
    rep = evt_report(losses, threshold, confidence_levels=(0.95, 0.99))
    d = rep.to_dict()
    assert "gpd" in d and "var" in d and "cvar" in d
    assert "0.95" in d["var"] and "0.99" in d["var"]


def test_evt_report_empty_raises():
    with pytest.raises(ValueError):
        evt_report([], 0.5)


def test_evt_var_few_exceedances_fallback():
    # <2 exceedances → falls back to empirical max (no crash)
    losses = [0.1, 0.2, 0.3, 0.4, 0.5]
    v = evt_var(losses, threshold=0.45, alpha=0.99)
    assert v == 0.5  # empirical max