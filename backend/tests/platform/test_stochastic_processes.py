"""Tests for P246 stochastic processes / SDE."""

from __future__ import annotations

import math

import pytest

from app.platform.stochastic_processes import (
    cir_moments,
    cir_simulate,
    gbm_moments,
    gbm_simulate,
    merton_jd_moments,
    merton_jd_simulate,
    ou_moments,
    ou_simulate,
)


def _mean(xs):
    return sum(xs) / len(xs)


def _var(xs):
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def test_gbm_path_length_and_start():
    res = gbm_simulate(100.0, 0.05, 0.2, 1.0, 50, seed=1)
    assert len(res.path) == 51
    assert res.path[0] == 100.0
    assert all(p > 0 for p in res.path)


def test_gbm_deterministic_with_seed():
    a = gbm_simulate(100.0, 0.05, 0.2, 1.0, 30, seed=42)
    b = gbm_simulate(100.0, 0.05, 0.2, 1.0, 30, seed=42)
    assert a.path == b.path


def test_gbm_log_mean_matches_analytic():
    S0, mu, sigma, T, n = 100.0, 0.08, 0.2, 1.0, 100
    finals = []
    for s in range(200):
        res = gbm_simulate(S0, mu, sigma, T, n, seed=s)
        finals.append(math.log(res.path[-1]))
    mom = gbm_moments(S0, mu, sigma, T)
    assert abs(_mean(finals) - mom["mean_log"]) < 0.05


def test_gbm_log_var_matches_analytic():
    S0, mu, sigma, T, n = 100.0, 0.08, 0.2, 1.0, 100
    finals = [math.log(gbm_simulate(S0, mu, sigma, T, n, seed=s).path[-1]) for s in range(300)]
    mom = gbm_moments(S0, mu, sigma, T)
    assert abs(_var(finals) - mom["var_log"]) < 0.02


def test_ou_stationary_variance():
    res = ou_simulate(0.0, 1.0, 0.0, 0.3, 50.0, 5000, seed=7)
    mom = ou_moments(0.0, 1.0, 0.0, 0.3, 50.0)
    sample = res.path[2000:]
    assert abs(_var(sample) - mom["stationary_var"]) < 0.01


def test_ou_mean_reverts_to_theta():
    res = ou_simulate(5.0, 2.0, 1.0, 0.2, 5.0, 1000, seed=3)
    assert abs(_mean(res.path[500:]) - 1.0) < 0.1


def test_cir_positivity_feller_satisfied():
    res = cir_simulate(0.05, 2.0, 0.05, 0.1, 5.0, 2000, seed=11)
    assert all(r >= 0.0 for r in res.path)
    mom = cir_moments(0.05, 2.0, 0.05, 0.1, 5.0)
    assert mom["feller_satisfied"] == 1.0


def test_cir_stationary_mean():
    res = cir_simulate(0.1, 1.0, 0.04, 0.1, 50.0, 10000, seed=13)
    mom = cir_moments(0.1, 1.0, 0.04, 0.1, 50.0)
    assert abs(_mean(res.path[3000:]) - 0.04) < 0.02


def test_merton_jd_var_log_matches():
    lam, T = 5.0, 1.0
    finals = [
        math.log(merton_jd_simulate(100.0, 0.05, 0.15, lam, 0.0, 0.05, T, 100, seed=s).path[-1])
        for s in range(300)
    ]
    mom = merton_jd_moments(100.0, 0.05, 0.15, lam, 0.0, 0.05, T)
    assert abs(_var(finals) - mom["var_log"]) < 0.06


def test_merton_jd_zero_lambda_is_gbm():
    T = 1.0
    finals = [
        math.log(merton_jd_simulate(100.0, 0.05, 0.2, 0.0, 0.0, 0.0, T, 100, seed=s).path[-1])
        for s in range(300)
    ]
    mom = merton_jd_moments(100.0, 0.05, 0.2, 0.0, 0.0, 0.0, T)
    assert abs(_var(finals) - mom["var_log"]) < 0.03


def test_to_dict_roundtrip():
    res = gbm_simulate(50.0, 0.03, 0.15, 0.5, 10, seed=0)
    d = res.to_dict()
    assert d["process"] == "gbm"
    assert len(d["path"]) == 11


def test_invalid_params_raise():
    with pytest.raises(ValueError):
        gbm_simulate(-1.0, 0.05, 0.2, 1.0, 10)
    with pytest.raises(ValueError):
        gbm_simulate(100.0, 0.05, 0.0, 1.0, 10)
    with pytest.raises(ValueError):
        ou_simulate(0.0, 0.0, 0.0, 0.1, 1.0, 10)
    with pytest.raises(ValueError):
        cir_simulate(-0.1, 1.0, 0.05, 0.1, 1.0, 10)
    with pytest.raises(ValueError):
        gbm_simulate(100.0, 0.05, 0.2, 0.0, 10)