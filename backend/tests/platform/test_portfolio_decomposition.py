"""Tests for P239 portfolio decomposition (returns -> factors + residual)."""

from __future__ import annotations

import math

import pytest

from app.platform.portfolio_decomposition import (
    FactorExposureResult,
    ReturnDecomposition,
    VarianceDecomposition,
    decompose_return,
    returns_to_factors,
    variance_decomposition,
)


# ---------------------------------------------------------------------------
# returns_to_factors
# ---------------------------------------------------------------------------


def test_returns_to_factors_known_answer_single_factor():
    # r_t = 0.5 * f_t + 1.0 (exact linear, no residual) -> beta=0.5, alpha=1.0
    f = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    r = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
    res = returns_to_factors(r, {"mkt": f})
    assert isinstance(res, FactorExposureResult)
    assert abs(res.betas["mkt"] - 0.5) < 1e-9
    assert abs(res.alpha - 1.0) < 1e-9
    # exact fit -> R^2 == 1, residuals ~ 0, residual_contribution ~ 0
    assert abs(res.r_squared - 1.0) < 1e-9
    assert all(abs(e) < 1e-9 for e in res.residuals)
    assert abs(res.residual_contribution) < 1e-9
    # factor contribution = beta * mean(f) = 0.5 * 3.5 = 1.75
    assert abs(res.factor_contribution["mkt"] - 1.75) < 1e-9


def test_returns_to_factors_residual_capture():
    # r_t = 2.0 * f_t + zero-mean noise; use centered f so beta/alpha are clean.
    # beta == slope of r on f, alpha == mean(r) (since mean(f) == 0),
    # residual_contribution == mean(noise) == 0 by construction.
    f = [-2.0, -1.0, 0.0, 1.0, 2.0]
    noise = [0.02, -0.02, 0.0, -0.02, 0.02]  # zero-mean and orthogonal to f
    r = [2.0 * fi + ni for fi, ni in zip(f, noise)]
    res = returns_to_factors(r, {"mkt": f})
    assert abs(res.betas["mkt"] - 2.0) < 1e-9
    assert abs(res.alpha) < 1e-9
    assert abs(res.residual_contribution) < 1e-9
    for eps, ni in zip(res.residuals, noise):
        assert abs(eps - ni) < 1e-9


def test_returns_to_factors_two_factors_exact():
    # r_t = 1.0 * mkt + 2.0 * smb + 0.5 (exact)
    mkt = [0.5, 1.0, 1.5, 2.0, 2.5]
    smb = [0.1, -0.1, 0.2, -0.2, 0.0]
    r = [1.0 * m + 2.0 * s + 0.5 for m, s in zip(mkt, smb)]
    res = returns_to_factors(r, {"mkt": mkt, "smb": smb})
    assert abs(res.alpha - 0.5) < 1e-9
    assert abs(res.betas["mkt"] - 1.0) < 1e-9
    assert abs(res.betas["smb"] - 2.0) < 1e-9
    assert abs(res.r_squared - 1.0) < 1e-9


def test_returns_to_factors_empty_raises():
    with pytest.raises(ValueError):
        returns_to_factors([], {"mkt": [1.0]})


def test_returns_to_factors_empty_factors_raises():
    with pytest.raises(ValueError):
        returns_to_factors([1.0, 2.0], {})


def test_returns_to_factors_length_mismatch_raises():
    with pytest.raises(ValueError):
        returns_to_factors([1.0, 2.0, 3.0], {"mkt": [1.0, 2.0]})


def test_returns_to_factors_insufficient_obs_raises():
    # 1 factor -> need >=2 obs; only 1 provided
    with pytest.raises(ValueError):
        returns_to_factors([1.0], {"mkt": [2.0]})


def test_returns_to_factors_singular_design_raises():
    # two identical factor columns -> singular XtX
    f = [1.0, 2.0, 3.0, 4.0, 5.0]
    with pytest.raises(ValueError):
        returns_to_factors([0.5, 1.0, 1.5, 2.0, 2.5], {"a": f, "b": f})


def test_returns_to_factors_to_dict():
    res = returns_to_factors([1.0, 2.0, 3.0, 4.0], {"mkt": [0.1, 0.2, 0.3, 0.4]})
    d = res.to_dict()
    assert set(d.keys()) >= {
        "alpha",
        "betas",
        "r_squared",
        "residuals",
        "factor_contribution",
        "residual_contribution",
        "n",
        "factors",
    }
    assert d["n"] == 4
    assert d["factors"] == ["mkt"]


def test_returns_to_factors_contribution_sums_to_mean_return():
    # alpha + sum(factor_contribution) + residual_contribution == mean(r)
    mkt = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    smb = [0.1, -0.1, 0.2, -0.2, 0.0, 0.1]
    r = [0.8 * m + 1.2 * s + 0.3 + n for m, s, n in
         zip(mkt, smb, [0.01, -0.01, 0.02, -0.02, 0.0, 0.03])]
    res = returns_to_factors(r, {"mkt": mkt, "smb": smb})
    mean_r = sum(r) / len(r)
    reconstructed = (
        res.alpha
        + sum(res.factor_contribution.values())
        + res.residual_contribution
    )
    # alpha is a per-period constant; mean(alpha) == alpha, so reconstruction
    # of the *mean* return is alpha + sum(beta_i * mean(f_i)) + mean(eps).
    assert abs(reconstructed - mean_r) < 1e-9


# ---------------------------------------------------------------------------
# decompose_return
# ---------------------------------------------------------------------------


def test_decompose_return_reconciles_exact():
    d = decompose_return(
        total_return=0.10,
        contributions={"mkt": 0.06, "smb": 0.03},
        residual=0.01,
    )
    assert isinstance(d, ReturnDecomposition)
    assert abs(d.reconciliation_error) < 1e-12
    assert d.total_return == pytest.approx(0.10)
    assert list(d.contributions.keys()) == ["mkt", "smb"]


def test_decompose_return_mismatch_records_error():
    d = decompose_return(
        total_return=0.10,
        contributions={"mkt": 0.06, "smb": 0.03},
        residual=0.005,  # 0.06 + 0.03 + 0.005 = 0.095 != 0.10
    )
    assert abs(d.reconciliation_error - 0.005) < 1e-12


def test_decompose_return_ordering_preserved():
    d = decompose_return(
        total_return=0.0,
        contributions={"z": 0.0, "a": 0.0, "m": 0.0},
        residual=0.0,
    )
    assert list(d.contributions.keys()) == ["z", "a", "m"]


def test_decompose_return_to_dict():
    d = decompose_return(0.05, {"mkt": 0.04}, 0.01)
    dd = d.to_dict()
    assert dd["total_return"] == 0.05
    assert dd["contributions"] == {"mkt": 0.04}
    assert dd["residual"] == 0.01
    assert dd["reconciliation_error"] >= 0.0


# ---------------------------------------------------------------------------
# variance_decomposition
# ---------------------------------------------------------------------------


def test_variance_decomposition_single_factor_known_answer():
    # w = [0.5, 0.5], beta = [1.0, 1.0], F = [[0.04]], idio_var = 0.01
    # systematic = (sum w_i b_i)^2 * F = (0.5*1 + 0.5*1)^2 * 0.04 = 1.0 * 0.04 = 0.04
    # idio = 0.01 * (0.25 + 0.25) = 0.005
    # total = 0.045
    res = variance_decomposition(
        weights=[0.5, 0.5],
        factor_cov=[[0.04]],
        factor_exposures=[1.0, 1.0],
        idio_var=0.01,
    )
    assert isinstance(res, VarianceDecomposition)
    assert abs(res.systematic_var - 0.04) < 1e-9
    assert abs(res.idiosyncratic_var - 0.005) < 1e-9
    assert abs(res.total_var - 0.045) < 1e-9
    assert abs(res.systematic_share - (0.04 / 0.045)) < 1e-9
    assert abs(res.idiosyncratic_share - (0.005 / 0.045)) < 1e-9


def test_variance_decomposition_per_factor_contribution_sums_to_systematic():
    res = variance_decomposition(
        weights=[0.3, 0.7],
        factor_cov=[[0.09]],
        factor_exposures=[1.0, 1.0],
        idio_var=0.02,
    )
    assert abs(
        sum(res.per_factor_contribution.values()) - res.systematic_var
    ) < 1e-9
    # shares sum to 1
    assert abs(
        sum(res.per_factor_share.values())
        - (res.systematic_share + res.idiosyncratic_share)
    ) < 1e-9 or abs(sum(res.per_factor_share.values()) - res.systematic_share) < 1e-9


def test_variance_decomposition_bt_w_path():
    # multi-factor via Bᵀw path: factor_exposures already = Bᵀw (k-vector)
    # weights length n=3, factor_cov 2x2, exposures length 2 == k
    res = variance_decomposition(
        weights=[0.4, 0.3, 0.3],
        factor_cov=[[0.04, 0.01], [0.01, 0.09]],
        factor_exposures=[0.5, 0.2],  # Bᵀw
        idio_var=0.01,
    )
    # systematic = BtW . F . BtW
    bw = [0.5, 0.2]
    fb = [0.04 * 0.5 + 0.01 * 0.2, 0.01 * 0.5 + 0.09 * 0.2]
    sys_expect = bw[0] * fb[0] + bw[1] * fb[1]
    assert abs(res.systematic_var - sys_expect) < 1e-9
    # idio = 0.01 * sum(w^2)
    idio_expect = 0.01 * (0.16 + 0.09 + 0.09)
    assert abs(res.idiosyncratic_var - idio_expect) < 1e-9


def test_variance_decomposition_empty_weights_raises():
    with pytest.raises(ValueError):
        variance_decomposition([], [[0.1]], [], 0.01)


def test_variance_decomposition_exposure_length_mismatch_raises():
    with pytest.raises(ValueError):
        variance_decomposition(
            weights=[0.5, 0.5],
            factor_cov=[[0.04]],
            factor_exposures=[1.0, 1.0, 1.0],  # length 3 != 2
            idio_var=0.01,
        )


def test_variance_decomposition_non_square_factor_cov_raises():
    with pytest.raises(ValueError):
        variance_decomposition(
            weights=[0.5, 0.5],
            factor_cov=[[0.04, 0.01]],  # 1x2 not square
            factor_exposures=[1.0, 1.0],
            idio_var=0.01,
        )


def test_variance_decomposition_empty_factor_cov_raises():
    with pytest.raises(ValueError):
        variance_decomposition(
            weights=[0.5, 0.5],
            factor_cov=[],
            factor_exposures=[1.0, 1.0],
            idio_var=0.01,
        )


def test_variance_decomposition_shape_mismatch_k_ne_n_raises():
    # k=2, n=2 -> ambiguous; exposures length n=2 == k=2 but also == n,
    # so it would take the BtW path. Force a real mismatch: k=2, n=3,
    # exposures length 3 != k=2 and k != 1
    with pytest.raises(ValueError):
        variance_decomposition(
            weights=[0.4, 0.3, 0.3],
            factor_cov=[[0.04, 0.01], [0.01, 0.09]],
            factor_exposures=[1.0, 1.0, 1.0],  # length 3 != k=2
            idio_var=0.01,
        )


def test_variance_decomposition_to_dict_keys():
    res = variance_decomposition(
        weights=[0.5, 0.5],
        factor_cov=[[0.04]],
        factor_exposures=[1.0, 1.0],
        idio_var=0.01,
    )
    d = res.to_dict()
    assert set(d.keys()) == {
        "systematic_var",
        "idiosyncratic_var",
        "total_var",
        "systematic_share",
        "idiosyncratic_share",
        "per_factor_contribution",
        "per_factor_share",
    }
    assert "factor_0" in d["per_factor_contribution"]


def test_variance_decomposition_zero_total_shares_zero():
    # weights = 0 -> systematic = 0, idio = 0, total = 0 -> shares = 0
    res = variance_decomposition(
        weights=[0.0, 0.0],
        factor_cov=[[0.04]],
        factor_exposures=[1.0, 1.0],
        idio_var=0.01,
    )
    assert res.total_var == 0.0
    assert res.systematic_share == 0.0
    assert res.idiosyncratic_share == 0.0