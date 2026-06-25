"""Tests for P230 factor risk decomposition."""

from __future__ import annotations

import math

import pytest

from app.platform.factor_risk import (
    factor_risk_decomposition,
    gram_matrix,
    matrix_mult_vec,
)


def test_matrix_mult_vec_basic():
    M = [[1.0, 2.0], [3.0, 4.0]]
    v = [1.0, 1.0]
    assert matrix_mult_vec(M, v) == [3.0, 7.0]


def test_matrix_mult_vec_dim_mismatch():
    with pytest.raises(ValueError):
        matrix_mult_vec([[1.0, 2.0]], [1.0])


def test_gram_matrix_identity():
    # B = [[1,0],[0,1]] (2x2), F = identity 2x2 → B F B^T = identity
    B = [[1.0, 0.0], [0.0, 1.0]]
    F = [[1.0, 0.0], [0.0, 1.0]]
    G = gram_matrix(B, F)
    assert abs(G[0][0] - 1.0) < 1e-9
    assert abs(G[1][1] - 1.0) < 1e-9
    assert abs(G[0][1]) < 1e-9


def test_gram_matrix_invalid_dims():
    with pytest.raises(ValueError):
        gram_matrix([[1.0, 2.0, 3.0]], [[1.0, 0.0], [0.0, 1.0]])


def test_factor_risk_decomposition_pure_factor():
    # single asset, single factor, beta=1, factor_var=0.04, idio=0.0
    # portfolio variance = factor variance = 0.04 (since BtW=1, FBtW=0.04)
    res = factor_risk_decomposition(
        weights={"A": 1.0},
        exposures={"A": {"MKT": 1.0}},
        factor_cov={"MKT": {"MKT": 0.04}},
        idio_var={"A": 0.0},
    )
    assert abs(res.portfolio_variance - 0.04) < 1e-9
    assert abs(res.factor_variance - 0.04) < 1e-9
    assert res.idiosyncratic_variance == 0.0
    assert abs(res.factor_share - 1.0) < 1e-9


def test_factor_risk_decomposition_pure_idio():
    # no factor exposure → all idio
    res = factor_risk_decomposition(
        weights={"A": 1.0},
        exposures={"A": {"MKT": 0.0}},
        factor_cov={"MKT": {"MKT": 0.04}},
        idio_var={"A": 0.09},
    )
    assert abs(res.portfolio_variance - 0.09) < 1e-9
    assert abs(res.factor_variance - 0.0) < 1e-9
    assert abs(res.factor_share - 0.0) < 1e-9


def test_factor_risk_decomposition_mixed():
    # two assets, one factor, diversified idio
    res = factor_risk_decomposition(
        weights={"A": 0.5, "B": 0.5},
        exposures={"A": {"MKT": 1.2}, "B": {"MKT": 0.8}},
        factor_cov={"MKT": {"MKT": 0.04}},
        idio_var={"A": 0.01, "B": 0.01},
    )
    # BtW = 0.5*1.2 + 0.5*0.8 = 1.0; factor var = 1.0 * 0.04 * 1.0 = 0.04
    assert abs(res.factor_variance - 0.04) < 1e-9
    # idio = 0.5^2*0.01 + 0.5^2*0.01 = 0.005
    assert abs(res.idiosyncratic_variance - 0.005) < 1e-9
    assert abs(res.portfolio_variance - 0.045) < 1e-9


def test_factor_risk_decomposition_to_dict():
    res = factor_risk_decomposition(
        weights={"A": 1.0},
        exposures={"A": {"MKT": 1.0}},
        factor_cov={"MKT": {"MKT": 0.04}},
        idio_var={"A": 0.0},
    )
    d = res.to_dict()
    assert "per_factor_variance" in d and "per_factor_share" in d


def test_factor_risk_decomposition_empty_weights():
    with pytest.raises(ValueError):
        factor_risk_decomposition(
            weights={},
            exposures={},
            factor_cov={"MKT": {"MKT": 0.04}},
            idio_var={},
        )


def test_factor_risk_decomposition_empty_factors():
    with pytest.raises(ValueError):
        factor_risk_decomposition(
            weights={"A": 1.0},
            exposures={"A": {}},
            factor_cov={},
            idio_var={"A": 0.0},
        )


def test_factor_risk_decomposition_multi_factor_orthogonal():
    # two orthogonal factors
    res = factor_risk_decomposition(
        weights={"A": 1.0},
        exposures={"A": {"F1": 1.0, "F2": 1.0}},
        factor_cov={"F1": {"F1": 0.01, "F2": 0.0}, "F2": {"F1": 0.0, "F2": 0.02}},
        idio_var={"A": 0.0},
    )
    # BtW = [1,1], F·BtW = [0.01, 0.02], per-factor var = [0.01, 0.02]
    assert abs(res.per_factor_variance["F1"] - 0.01) < 1e-9
    assert abs(res.per_factor_variance["F2"] - 0.02) < 1e-9
    assert abs(res.factor_variance - 0.03) < 1e-9