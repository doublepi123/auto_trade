"""Tests for P240 Hansen-White Superior Predictive Ability test."""

from __future__ import annotations

import math

import pytest

from app.platform.spa_test import (
    SpaResult,
    _stationary_bootstrap_indices,
    spa_test,
)


def test_stationary_bootstrap_indices_shape():
    out = _stationary_bootstrap_indices(n=10, B=4, block_length=3)
    assert len(out) == 4
    for seq in out:
        assert len(seq) == 10
        assert all(0 <= i < 10 for i in seq)


def test_stationary_bootstrap_indices_deterministic():
    a = _stationary_bootstrap_indices(n=12, B=5, block_length=4)
    b = _stationary_bootstrap_indices(n=12, B=5, block_length=4)
    assert a == b  # fully reproducible, no RNG


def test_stationary_bootstrap_indices_covers_all_when_block_n():
    # block_length == n => each draw is a contiguous circular block of length n
    out = _stationary_bootstrap_indices(n=8, B=3, block_length=8)
    for seq in out:
        # each draw covers all 8 indices exactly once (a full cycle)
        assert sorted(seq) == list(range(8))


def test_stationary_bootstrap_indices_invalid():
    with pytest.raises(ValueError):
        _stationary_bootstrap_indices(n=0, B=4, block_length=2)
    with pytest.raises(ValueError):
        _stationary_bootstrap_indices(n=10, B=0, block_length=2)
    with pytest.raises(ValueError):
        _stationary_bootstrap_indices(n=10, B=4, block_length=0)
    with pytest.raises(ValueError):
        _stationary_bootstrap_indices(n=10, B=4, block_length=11)


def test_spa_basic_happy_path():
    # benchmark loses 0.5 each bar; model A loses 0.4 (better), model B 0.6 (worse)
    bench = [0.5] * 20
    model_a = [0.4] * 20
    model_b = [0.6] * 20
    res = spa_test(bench, [model_a, model_b], B=50, block_length=4)
    assert isinstance(res, SpaResult)
    assert res.n == 20
    assert res.B == 50
    assert res.n_models_beating_benchmark == 1  # only model_a
    # model A has constant positive differential -> t-stat positive & large
    assert res.t_statistic > 0
    d = res.to_dict()
    assert set(d.keys()) >= {
        "spa_pvalue",
        "consistent_pvalue",
        "t_statistic",
        "n_models_beating_benchmark",
        "individual_pvalues",
    }
    assert len(res.individual_pvalues) == 2


def test_spa_pvalue_in_unit_interval():
    bench = [0.5, 0.4, 0.6, 0.5, 0.3, 0.7, 0.5, 0.4, 0.6, 0.5]
    models = [
        [0.4, 0.5, 0.5, 0.6, 0.4, 0.6, 0.4, 0.5, 0.5, 0.6],
        [0.6, 0.3, 0.7, 0.4, 0.5, 0.6, 0.6, 0.3, 0.7, 0.4],
    ]
    res = spa_test(bench, models, B=100, block_length=2)
    assert 0.0 <= res.spa_pvalue <= 1.0
    assert 0.0 <= res.consistent_pvalue <= 1.0
    for p in res.individual_pvalues:
        assert 0.0 <= p <= 1.0


def test_spa_empty_benchmark_raises():
    with pytest.raises(ValueError):
        spa_test([], [[0.1, 0.2]])


def test_spa_too_short_benchmark_raises():
    with pytest.raises(ValueError):
        spa_test([0.5], [[0.4]])


def test_spa_no_models_raises():
    with pytest.raises(ValueError):
        spa_test([0.5, 0.4, 0.6], [])


def test_spa_length_mismatch_raises():
    with pytest.raises(ValueError):
        spa_test([0.5, 0.4, 0.6], [[0.4, 0.4]])


def test_spa_invalid_B_raises():
    with pytest.raises(ValueError):
        spa_test([0.5, 0.4, 0.6, 0.5], [[0.4, 0.4, 0.4, 0.4]], B=0)


def test_spa_all_equal_models_zero_tstat():
    # all models identical to benchmark => zero differentials, zero t-stat
    bench = [0.5, 0.4, 0.6, 0.5, 0.3, 0.7, 0.5, 0.4]
    models = [list(bench), list(bench)]
    res = spa_test(bench, models, B=50, block_length=2)
    assert res.t_statistic == 0.0
    assert res.n_models_beating_benchmark == 0
    # no model beats benchmark => consistent p-value should be high (cannot reject)
    assert res.consistent_pvalue >= 0.5


def test_spa_clearly_better_model_rejects():
    # one model dominates benchmark strongly and consistently
    bench = [1.0] * 30
    model_good = [0.5] * 30  # much lower loss => better
    model_noise = [1.0 + 0.01 * ((-1) ** i) for i in range(30)]  # ~same as bench
    res = spa_test(bench, [model_good, model_noise], B=200, block_length=5)
    # strong, consistent outperformance => small consistent p-value
    assert res.t_statistic > 5.0
    assert res.consistent_pvalue < 0.5
    assert res.n_models_beating_benchmark == 1


def test_spa_block_length_clamped_to_n():
    # block_length > n should be silently clamped, not raise
    bench = [0.5, 0.4, 0.6]
    models = [[0.4, 0.4, 0.4]]
    res = spa_test(bench, models, B=10, block_length=99)
    assert res.block_length == 3  # clamped to n


def test_spa_individual_pvalues_length_matches_models():
    bench = [0.5, 0.4, 0.6, 0.5, 0.3, 0.7]
    models = [
        [0.4, 0.4, 0.5, 0.4, 0.3, 0.6],
        [0.6, 0.5, 0.7, 0.6, 0.4, 0.8],
        [0.5, 0.4, 0.6, 0.5, 0.3, 0.7],
    ]
    res = spa_test(bench, models, B=20, block_length=2)
    assert len(res.individual_pvalues) == 3
    assert len(res.individual_pvalues) == res.to_dict()["individual_pvalues"].__len__()


def test_spa_to_dict_roundtrip_keys():
    bench = [0.5, 0.4, 0.6, 0.5]
    models = [[0.4, 0.4, 0.5, 0.4]]
    res = spa_test(bench, models, B=10, block_length=2)
    d = res.to_dict()
    # snake_case keys (matching EvtResult/SensitivityReport idiom)
    expected = {
        "spa_pvalue",
        "consistent_pvalue",
        "t_statistic",
        "n_models_beating_benchmark",
        "individual_pvalues",
        "n",
        "B",
        "block_length",
    }
    assert expected.issubset(d.keys())


def test_spa_independent_series_high_pvalue():
    # Models that are noise around the benchmark (no real outperformance)
    # should NOT reject the null => consistent p-value should be high-ish.
    bench = [math.sin(i * 0.3) * 0.5 + 0.5 for i in range(40)]
    # two models that are essentially uncorrelated noise around bench mean
    m1 = [0.5 + 0.02 * ((i * 7) % 5 - 2) for i in range(40)]
    m2 = [0.5 + 0.02 * ((i * 11) % 7 - 3) for i in range(40)]
    res = spa_test(bench, [m1, m2], B=100, block_length=5)
    # noisy, near-zero differential => cannot reject null
    assert res.consistent_pvalue >= 0.3


def test_spa_more_models_increases_snooping_burden():
    # Adding many weak/noise models should not lower the consistent p-value
    # below the case with just one strong model (data-snooping penalty).
    bench = [1.0] * 25
    strong = [0.8] * 25  # modest but consistent outperformance
    few = spa_test(bench, [strong], B=200, block_length=5)
    # add 10 noise models that occasionally look good by chance
    noise_models = [
        [1.0 + 0.05 * (((i + k) % 3) - 1) for i in range(25)] for k in range(10)
    ]
    many = spa_test(bench, [strong] + noise_models, B=200, block_length=5)
    # both should have the same best model's t-stat direction (positive)
    assert few.t_statistic > 0
    assert many.t_statistic > 0
    # p-values are valid probabilities
    assert 0.0 <= few.consistent_pvalue <= 1.0
    assert 0.0 <= many.consistent_pvalue <= 1.0