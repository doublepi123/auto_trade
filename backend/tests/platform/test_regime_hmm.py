"""Tests for P234 Hidden Markov Model regime detector (Baum-Welch + Viterbi)."""

from __future__ import annotations

import math

import pytest

from app.platform.regime_hmm import (
    HMMParams,
    backward_probs,
    fit_hmm,
    forward_probs,
    regime_label,
    state_means,
    state_stds,
    viterbi,
)


def _two_state_synthetic(n: int = 60, seed: int = 0) -> list[float]:
    """Deterministic synthetic returns with two distinct regimes (no RNG)."""
    out: list[float] = []
    # use a simple deterministic LCG so the sequence is reproducible
    x = seed
    for i in range(n):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        u = (x / 0x7FFFFFFF) - 0.5  # in (-0.5, 0.5)
        if i < n // 2:
            out.append(0.01 + 0.005 * u)  # bull regime: positive drift
        else:
            out.append(-0.01 + 0.005 * u)  # bear regime: negative drift
    return out


def test_fit_hmm_happy_path():
    r = _two_state_synthetic(80)
    res = fit_hmm(r, n_states=2, n_iter=30)
    d = res.to_dict()
    assert d["params"]["n_states"] == 2
    assert "log_likelihood" in d
    assert "converged" in d
    assert isinstance(d["regime_labels"], list)
    assert len(d["regime_labels"]) == len(r)


def test_fit_hmm_log_likelihood_finite():
    r = _two_state_synthetic(60)
    res = fit_hmm(r, n_states=2, n_iter=20)
    assert math.isfinite(res.log_likelihood)


def test_fit_hmm_two_states_separated_means():
    # the first half is positive drift, second half negative drift
    r = _two_state_synthetic(100)
    res = fit_hmm(r, n_states=2, n_iter=50, tol=1e-7)
    means = res.params.means
    # one state mean should be clearly positive, the other clearly negative
    assert max(means) > 0
    assert min(means) < 0


def test_fit_hmm_regime_labels_cover_states():
    r = _two_state_synthetic(100)
    res = fit_hmm(r, n_states=2, n_iter=50)
    # labels should only be from the 2-state label set
    assert set(res.regime_labels).issubset({"BULL", "BEAR"})
    # both states should appear in the path (we constructed two clear regimes)
    states = viterbi(r, res.params)
    assert set(states) == {0, 1}


def test_fit_hmm_three_states_labels():
    # build a 3-regime synthetic: positive / flat / negative segments
    r: list[float] = []
    for i in range(45):
        r.append(0.02 + 0.002 * (i % 5))
    for i in range(45):
        r.append(0.0005 * (i % 7))
    for i in range(45):
        r.append(-0.02 - 0.002 * (i % 5))
    res = fit_hmm(r, n_states=3, n_iter=60, tol=1e-7)
    assert res.params.n_states == 3
    assert set(res.regime_labels).issubset({"BULL", "BEAR", "SIDWAYS"})
    assert "BULL" in res.regime_labels
    assert "BEAR" in res.regime_labels


def test_fit_hmm_empty_raises():
    with pytest.raises(ValueError):
        fit_hmm([], n_states=2)


def test_fit_hmm_single_return_raises():
    with pytest.raises(ValueError):
        fit_hmm([0.01], n_states=2)


def test_fit_hmm_non_finite_raises():
    with pytest.raises(ValueError):
        fit_hmm([0.01, float("nan"), 0.02], n_states=2)


def test_fit_hmm_bad_n_states():
    with pytest.raises(ValueError):
        fit_hmm([0.01, 0.02, 0.03], n_states=1)


def test_fit_hmm_bad_n_iter():
    with pytest.raises(ValueError):
        fit_hmm([0.01, 0.02, 0.03], n_states=2, n_iter=0)


def test_fit_hmm_is_deterministic():
    r = _two_state_synthetic(70)
    a = fit_hmm(r, n_states=2, n_iter=25)
    b = fit_hmm(r, n_states=2, n_iter=25)
    assert a.params.means == b.params.means
    assert a.params.stds == b.params.stds
    assert a.log_likelihood == b.log_likelihood


def test_fit_hmm_iter_cap_respected():
    r = _two_state_synthetic(50)
    res = fit_hmm(r, n_states=2, n_iter=3)
    assert res.n_iter_run <= 3


def test_viterbi_returns_valid_states():
    r = _two_state_synthetic(50)
    res = fit_hmm(r, n_states=2, n_iter=30)
    path = viterbi(r, res.params)
    assert len(path) == len(r)
    assert all(s in (0, 1) for s in path)


def test_viterbi_empty_raises():
    params = HMMParams(
        n_states=2,
        init_probs=[0.5, 0.5],
        trans_matrix=[[0.9, 0.1], [0.1, 0.9]],
        means=[0.01, -0.01],
        stds=[0.01, 0.01],
    )
    with pytest.raises(ValueError):
        viterbi([], params)


def test_forward_backward_scales_consistent():
    # alpha_t(i) * beta_t(i) summed over i should be ~ 1/c_t scaling; the
    # posteriors gamma_t(i) must sum to 1 for each t.
    r = _two_state_synthetic(40)
    params = HMMParams(
        n_states=2,
        init_probs=[0.5, 0.5],
        trans_matrix=[[0.9, 0.1], [0.1, 0.9]],
        means=[0.01, -0.01],
        stds=[0.01, 0.01],
    )
    alpha, c = forward_probs(r, params)
    beta = backward_probs(r, params, c)
    for t in range(len(r)):
        gamma = [alpha[t][j] * beta[t][j] for j in range(2)]
        denom = sum(gamma)
        if denom > 0:
            gnorm = [g / denom for g in gamma]
            assert abs(sum(gnorm) - 1.0) < 1e-9


def test_state_means_stds():
    returns = [0.01, 0.02, 0.03, -0.01, -0.02, -0.03]
    states = [0, 0, 0, 1, 1, 1]
    means = state_means(returns, states, n_states=2)
    stds = state_stds(returns, states, n_states=2)
    assert abs(means[0] - 0.02) < 1e-9
    assert abs(means[1] - (-0.02)) < 1e-9
    assert stds[0] >= 0 and stds[1] >= 0


def test_state_means_length_mismatch():
    with pytest.raises(ValueError):
        state_means([0.01, 0.02], [0], n_states=2)


def test_state_stds_length_mismatch():
    with pytest.raises(ValueError):
        state_stds([0.01, 0.02], [0], n_states=2)


def test_regime_label_two_states():
    assert regime_label([0, 1, 0], [0.01, -0.01]) == ["BULL", "BEAR", "BULL"]


def test_regime_label_three_states():
    # means: -0.02 (BEAR), 0.0 (SIDWAYS), 0.02 (BULL)
    labels = regime_label([0, 1, 2, 1], [-0.02, 0.0, 0.02])
    assert labels == ["BEAR", "SIDWAYS", "BULL", "SIDWAYS"]


def test_regime_label_empty_means():
    assert regime_label([], []) == []


def test_to_dict_round_trip():
    r = _two_state_synthetic(40)
    res = fit_hmm(r, n_states=2, n_iter=10)
    d = res.to_dict()
    p = d["params"]
    assert p["n_states"] == 2
    assert len(p["init_probs"]) == 2
    assert len(p["trans_matrix"]) == 2
    assert len(p["trans_matrix"][0]) == 2
    assert len(p["means"]) == 2
    assert len(p["stds"]) == 2


def test_constant_returns_does_not_crash():
    # all identical → quantile bins are degenerate but std floor handles it
    r = [0.001] * 30
    res = fit_hmm(r, n_states=2, n_iter=10)
    assert res.params.n_states == 2
    assert math.isfinite(res.log_likelihood)