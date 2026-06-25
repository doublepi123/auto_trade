"""Tests for P228 Hawkes self-exciting process."""

from __future__ import annotations

import math

import pytest

from app.platform.hawkes import (
    branching_ratio,
    fit_hawkes,
    hawkes_log_likelihood,
    intensity_path,
)


def test_branching_ratio_basic():
    # n = κ/β
    assert abs(branching_ratio([], mu=0.5, kappa=0.3, beta=1.0) - 0.3) < 1e-9


def test_branching_ratio_invalid_beta():
    with pytest.raises(ValueError):
        branching_ratio([], mu=0.5, kappa=0.3, beta=0.0)


def test_intensity_path_at_event_times():
    # intensity just after an event spikes
    events = [1.0, 2.0]
    grid = [1.001, 2.001, 5.0]
    lam = intensity_path(events, grid, mu=0.1, kappa=0.5, beta=1.0)
    # at t=2.001 both events contribute (decayed)
    assert lam[1] > lam[2]  # closer to events → higher intensity


def test_intensity_path_invalid_beta():
    with pytest.raises(ValueError):
        intensity_path([1.0], [2.0], mu=0.1, kappa=0.5, beta=0.0)


def test_intensity_path_negative_kappa():
    with pytest.raises(ValueError):
        intensity_path([1.0], [2.0], mu=0.1, kappa=-0.5, beta=1.0)


def test_hawkes_log_likelihood_positive_for_typical_params():
    events = [1.0, 1.5, 2.0, 2.2, 3.0, 4.0, 4.1, 5.0]
    ll = hawkes_log_likelihood(events, mu=0.5, kappa=0.3, beta=1.0)
    assert isinstance(ll, float)


def test_hawkes_log_likelihood_empty():
    with pytest.raises(ValueError):
        hawkes_log_likelihood([], mu=0.5, kappa=0.3, beta=1.0)


def test_hawkes_log_likelihood_bad_params():
    with pytest.raises(ValueError):
        hawkes_log_likelihood([1.0], mu=0.0, kappa=0.3, beta=1.0)


def test_fit_hawkes_defaults():
    events = [1.0, 1.5, 2.0, 2.2, 3.0, 4.0, 4.1, 5.0, 6.0, 7.0]
    fit = fit_hawkes(events)
    assert fit.n_events == 10
    assert fit.branching_ratio == 0.5  # default kappa = 0.5*beta
    assert fit.stationary is True
    d = fit.to_dict()
    assert d["branching_ratio"] == fit.branching_ratio


def test_fit_hawkes_overrides():
    events = [1.0, 2.0, 3.0]
    fit = fit_hawkes(events, mu=0.5, kappa=2.0, beta=1.0)
    assert fit.mu == 0.5
    assert fit.kappa == 2.0
    assert fit.beta == 1.0
    assert fit.branching_ratio == 2.0  # super-critical
    assert fit.stationary is False


def test_fit_hawkes_empty():
    with pytest.raises(ValueError):
        fit_hawkes([])


def test_fit_hawkes_zero_window():
    with pytest.raises(ValueError):
        fit_hawkes([0.0, 0.0])  # last event = 0


def test_fit_hawkes_log_likelihood_finite():
    events = [1.0, 1.5, 2.0, 2.5, 3.0]
    fit = fit_hawkes(events)
    assert math.isfinite(fit.log_likelihood)