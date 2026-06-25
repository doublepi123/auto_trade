"""Tests for P252 vine copula."""

from __future__ import annotations

import math

import pytest

from app.platform.vine_copula import vine_copula


def _gaussian_panel(n: int, seed: int = 0, rho: float = 0.5) -> list[list[float]]:
    """Generate a 3-asset correlated return panel via Cholesky-free mixing."""
    import random
    rng = random.Random(seed)
    out = [[], [], []]
    for _ in range(n):
        z0 = rng.gauss(0.0, 1.0)
        z1 = rng.gauss(0.0, 1.0)
        z2 = rng.gauss(0.0, 1.0)
        # asset 1 = rho*z0 + sqrt(1-rho^2)*z1; asset 2 shares z0.
        out[0].append(z0)
        out[1].append(rho * z0 + math.sqrt(1 - rho * rho) * z1)
        out[2].append(rho * z0 + math.sqrt(1 - rho * rho) * z2)
    return out


def test_vine_copula_c_vine_3_assets():
    data = _gaussian_panel(200, seed=1, rho=0.6)
    res = vine_copula(data, structure="c-vine")
    assert res.structure == "c-vine"
    assert res.n_assets == 3
    assert len(res.pairs) == 3  # 3 pairs for 3 assets at level 1
    assert "aic" in res.to_dict() and "bic" in res.to_dict()


def test_vine_copula_d_vine_adjacent_pairs():
    data = _gaussian_panel(200, seed=2, rho=0.5)
    res = vine_copula(data, structure="d-vine")
    # D-vine level 1: pairs (0,1) and (1,2) only.
    assert len(res.pairs) == 2
    assert res.pairs[0]["asset_i"] == 0 and res.pairs[0]["asset_j"] == 1
    assert res.pairs[1]["asset_i"] == 1 and res.pairs[1]["asset_j"] == 2


def test_vine_copula_log_likelihood_finite():
    data = _gaussian_panel(150, seed=3, rho=0.4)
    res = vine_copula(data)
    assert math.isfinite(res.log_likelihood)
    assert all(math.isfinite(p["log_likelihood"]) for p in res.pairs)


def test_vine_copula_correlated_has_higher_tau_than_independent():
    strong = _gaussian_panel(300, seed=4, rho=0.9)
    weak = _gaussian_panel(300, seed=5, rho=0.0)
    rs = vine_copula(strong)
    rw = vine_copula(weak)
    # The pair (0,1) should have higher |tau| when correlated.
    tau_s = abs(rs.pairs[0]["kendall_tau"])
    tau_w = abs(rw.pairs[0]["kendall_tau"])
    assert tau_s > tau_w


def test_vine_copula_aic_bic_ordering():
    data = _gaussian_panel(200, seed=6, rho=0.5)
    # D-vine has fewer params than C-vine -> lower AIC/BIC for same log-lik family.
    rc = vine_copula(data, structure="c-vine")
    rd = vine_copula(data, structure="d-vine")
    assert rc.n_params > rd.n_params


def test_vine_copula_gaussian_family_explicit():
    data = _gaussian_panel(150, seed=7, rho=0.5)
    res = vine_copula(data, family="gaussian")
    assert all(p["family"] == "gaussian" for p in res.pairs)


def test_vine_copula_too_few_assets_raises():
    with pytest.raises(ValueError):
        vine_copula([[1.0, 2.0, 3.0]])


def test_vine_copula_ragged_raises():
    with pytest.raises(ValueError):
        vine_copula([[1.0, 2.0, 3.0], [1.0, 2.0]])


def test_vine_copula_constant_series_raises():
    with pytest.raises(ValueError):
        vine_copula([[1.0, 1.0, 1.0], [1.0, 2.0, 3.0]])


def test_vine_copula_unknown_structure_raises():
    data = _gaussian_panel(50, seed=8, rho=0.5)
    with pytest.raises(ValueError):
        vine_copula(data, structure="r-vine")


def test_vine_copula_too_few_obs_raises():
    with pytest.raises(ValueError):
        vine_copula([[1.0, 2.0], [2.0, 3.0], [3.0, 1.0]])


def test_to_dict_roundtrip():
    data = _gaussian_panel(100, seed=9, rho=0.5)
    d = vine_copula(data).to_dict()
    assert d["n_assets"] == 3
    assert "pairs" in d and len(d["pairs"]) >= 2