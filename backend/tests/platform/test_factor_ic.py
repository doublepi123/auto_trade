"""Tests for P264: factor IC analysis.

Pure-Python single-period cross-sectional factor Information Coefficient
diagnostics: Pearson IC, Spearman / rank IC, ICIR approximation, and
quantile-spread bucket decomposition. The module exposes no I/O and only
relies on the standard library.

Covers:
* pearson_corr positive on aligned factor/returns, zero-slope guard
* rank_values handles ties via average ranks
* spearman_corr / rank IC near 1 on monotonic ranks with ties
* factor_ic_report: quantile_spread positive when returns rise with factor
* buckets count sum == n and length == n_quantiles
* icir finite and well-defined
* invalid length mismatch / empty / n_quantiles bounds / bool entries /
  non-sequence inputs raise ValueError
* dataclasses are frozen and expose a to_dict

Endpoint-level coverage (200 / 422) lives in
``tests/platform/test_api_risk_portfolio.py``.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.platform.factor_ic import (
    FactorICResult,
    QuantileBucket,
    factor_ic_report,
    pearson_corr,
    rank_values,
    spearman_corr,
)


# ---------------------------------------------------------------------------
# pearson_corr
# ---------------------------------------------------------------------------


def test_pearson_corr_positive_on_aligned_series():
    factor = [1.0, 2.0, 3.0, 4.0, 5.0]
    returns = [0.01, 0.02, 0.03, 0.04, 0.05]
    ic = pearson_corr(factor, returns)
    assert ic == pytest.approx(1.0, abs=1e-12)


def test_pearson_corr_negative_on_inverted_series():
    factor = [1.0, 2.0, 3.0, 4.0, 5.0]
    returns = [0.05, 0.04, 0.03, 0.02, 0.01]
    ic = pearson_corr(factor, returns)
    assert ic == pytest.approx(-1.0, abs=1e-12)


def test_pearson_corr_zero_variance_returns_zero():
    # Constant series → zero variance → correlation is undefined → return 0.0.
    factor = [1.0, 2.0, 3.0, 4.0]
    returns = [0.02, 0.02, 0.02, 0.02]
    assert pearson_corr(factor, returns) == 0.0


def test_pearson_corr_length_mismatch_raises():
    with pytest.raises(ValueError):
        pearson_corr([1.0, 2.0, 3.0], [0.01, 0.02])


def test_pearson_corr_too_short_raises():
    with pytest.raises(ValueError):
        pearson_corr([1.0], [0.01])


def test_pearson_corr_bool_entry_raises():
    with pytest.raises(ValueError):
        pearson_corr([1.0, 2.0, True], [0.01, 0.02, 0.03])


def test_pearson_corr_non_finite_raises():
    with pytest.raises(ValueError):
        pearson_corr([1.0, float("nan"), 3.0], [0.01, 0.02, 0.03])


def test_pearson_corr_non_sequence_x_raises():
    # Bare scalar (non-iterable) is an invalid parameter → ValueError per spec.
    with pytest.raises(ValueError):
        pearson_corr(123, [0.01, 0.02, 0.03])  # type: ignore[arg-type]


def test_pearson_corr_non_sequence_y_raises():
    with pytest.raises(ValueError):
        pearson_corr([1.0, 2.0, 3.0], 123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# rank_values
# ---------------------------------------------------------------------------


def test_rank_values_strictly_increasing():
    ranks = rank_values([10.0, 20.0, 30.0, 40.0])
    assert ranks == [1.0, 2.0, 3.0, 4.0]


def test_rank_values_ties_average():
    # Two tied values at the lowest rank → both get (1 + 2) / 2 = 1.5.
    ranks = rank_values([5.0, 5.0, 10.0, 1.0])
    assert ranks == [2.5, 2.5, 4.0, 1.0]


def test_rank_values_all_equal():
    ranks = rank_values([7.0, 7.0, 7.0, 7.0])
    # All four tied → each gets (1+2+3+4)/4 = 2.5
    assert ranks == [2.5, 2.5, 2.5, 2.5]


def test_rank_values_invalid_short_raises():
    with pytest.raises(ValueError):
        rank_values([])


def test_rank_values_bool_raises():
    with pytest.raises(ValueError):
        rank_values([1.0, 2.0, True])


def test_rank_values_non_sequence_raises():
    # Bare scalar (non-iterable) is an invalid parameter → ValueError per spec.
    with pytest.raises(ValueError):
        rank_values(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# spearman_corr
# ---------------------------------------------------------------------------


def test_spearman_corr_perfect_monotonic_with_ties():
    # factor and returns are monotonically related but both contain ties.
    factor = [1.0, 1.0, 2.0, 3.0, 3.0]
    returns = [0.01, 0.01, 0.02, 0.03, 0.03]
    ic = spearman_corr(factor, returns)
    assert ic == pytest.approx(1.0, abs=1e-12)


def test_spearman_corr_zero_variance_returns_zero():
    factor = [1.0, 2.0, 3.0, 4.0]
    returns = [0.02, 0.02, 0.02, 0.02]
    assert spearman_corr(factor, returns) == 0.0


# ---------------------------------------------------------------------------
# factor_ic_report
# ---------------------------------------------------------------------------


def test_factor_ic_report_basic_monotonic():
    n = 20
    factor = [float(i) for i in range(n)]
    # Returns rise with factor → strong positive IC + positive spread.
    returns = [0.001 * i + 1e-6 * (i % 3) for i in range(n)]
    result = factor_ic_report(factor, returns, n_quantiles=5)
    assert isinstance(result, FactorICResult)
    assert result.pearson_ic > 0.95
    assert result.spearman_ic > 0.95
    assert result.rank_ic == pytest.approx(result.spearman_ic)
    assert result.quantile_spread > 0.0
    assert result.n == n
    assert len(result.buckets) == 5


def test_factor_ic_report_buckets_count_sum_equals_n():
    factor = [float(i) for i in range(20)]
    returns = [0.001 * i for i in range(20)]
    result = factor_ic_report(factor, returns, n_quantiles=4)
    assert len(result.buckets) == 4
    total = sum(b.count for b in result.buckets)
    assert total == 20


def test_factor_ic_report_quantile_spread_top_minus_bottom():
    # Top quantile should have a strictly higher mean than bottom when
    # returns are monotonic in factor.
    factor = [float(i) for i in range(10)]
    returns = [0.01 * i for i in range(10)]
    result = factor_ic_report(factor, returns, n_quantiles=2)
    bottom = result.buckets[0]
    top = result.buckets[-1]
    assert top.mean_return > bottom.mean_return
    assert result.quantile_spread == pytest.approx(
        top.mean_return - bottom.mean_return, abs=1e-12
    )


def test_factor_ic_report_quantile_spread_negative_for_inverted():
    factor = [float(i) for i in range(10)]
    returns = [-0.01 * i for i in range(10)]
    result = factor_ic_report(factor, returns, n_quantiles=2)
    assert result.quantile_spread < 0.0


def test_factor_ic_report_icir_finite():
    factor = [float(i) for i in range(15)]
    returns = [0.002 * i + 1e-5 * (i % 4) for i in range(15)]
    result = factor_ic_report(factor, returns, n_quantiles=3)
    assert math.isfinite(result.icir)


def test_factor_ic_report_n_quantiles_default_is_five():
    factor = [float(i) for i in range(20)]
    returns = [0.001 * i for i in range(20)]
    result = factor_ic_report(factor, returns)
    assert len(result.buckets) == 5


def test_factor_ic_report_uneven_buckets():
    # 11 samples, 4 quantiles → bucket sizes like 3/3/3/2 (or similar),
    # the contract is only that sum == n and length == n_quantiles.
    factor = [float(i) for i in range(11)]
    returns = [0.001 * i for i in range(11)]
    result = factor_ic_report(factor, returns, n_quantiles=4)
    assert len(result.buckets) == 4
    assert sum(b.count for b in result.buckets) == 11


def test_factor_ic_report_n_quantiles_equals_n():
    factor = [1.0, 2.0, 3.0]
    returns = [0.01, 0.02, 0.03]
    result = factor_ic_report(factor, returns, n_quantiles=3)
    assert len(result.buckets) == 3
    assert sum(b.count for b in result.buckets) == 3


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_factor_ic_report_length_mismatch_raises():
    with pytest.raises(ValueError):
        factor_ic_report([1.0, 2.0, 3.0], [0.01, 0.02], n_quantiles=2)


def test_factor_ic_report_empty_raises():
    with pytest.raises(ValueError):
        factor_ic_report([], [])


def test_factor_ic_report_single_sample_raises():
    with pytest.raises(ValueError):
        factor_ic_report([1.0], [0.01])


def test_factor_ic_report_n_quantiles_too_small_raises():
    with pytest.raises(ValueError):
        factor_ic_report([1.0, 2.0, 3.0, 4.0], [0.01, 0.02, 0.03, 0.04], n_quantiles=1)


def test_factor_ic_report_n_quantiles_greater_than_n_raises():
    with pytest.raises(ValueError):
        factor_ic_report([1.0, 2.0, 3.0], [0.01, 0.02, 0.03], n_quantiles=4)


def test_factor_ic_report_n_quantiles_bool_raises():
    with pytest.raises(ValueError):
        factor_ic_report(
            [1.0, 2.0, 3.0, 4.0],
            [0.01, 0.02, 0.03, 0.04],
            n_quantiles=True,  # type: ignore[arg-type]
        )


def test_factor_ic_report_bool_entry_raises():
    with pytest.raises(ValueError):
        factor_ic_report([1.0, True, 3.0], [0.01, 0.02, 0.03])


def test_factor_ic_report_non_finite_raises():
    with pytest.raises(ValueError):
        factor_ic_report([1.0, float("inf"), 3.0], [0.01, 0.02, 0.03])


def test_factor_ic_report_non_sequence_factor_raises():
    # A bare scalar (non-iterable) is an invalid parameter — ValueError, not
    # TypeError, per the P264 spec (all illegal args raise ValueError).
    with pytest.raises(ValueError):
        factor_ic_report(123, [0.01, 0.02, 0.03])  # type: ignore[arg-type]


def test_factor_ic_report_non_sequence_forward_returns_raises():
    # Bare scalar as forward_returns is also rejected with ValueError.
    with pytest.raises(ValueError):
        factor_ic_report([1.0, 2.0, 3.0], 123)  # type: ignore[arg-type]


def test_factor_ic_report_str_factor_raises():
    # str/bytes are iterable but are explicitly rejected as non-numeric sequences.
    with pytest.raises(ValueError):
        factor_ic_report("not a sequence", [0.01, 0.02, 0.03])  # type: ignore[arg-type]


def test_factor_ic_report_dict_entry_raises():
    with pytest.raises(ValueError):
        factor_ic_report([1.0, 2.0, {"x": 3.0}], [0.01, 0.02, 0.03])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Mapping / dict as the whole sequence must be rejected (P264 dict-key trap)
# ---------------------------------------------------------------------------


def test_rank_values_mapping_raises():
    # A bare dict/Mapping must NOT be silently treated as its keys sequence.
    with pytest.raises(ValueError):
        rank_values({1.0: "a", 2.0: "b"})  # type: ignore[arg-type]


def test_pearson_corr_mapping_x_raises():
    # A bare dict/Mapping must NOT be silently treated as its keys sequence.
    with pytest.raises(ValueError):
        pearson_corr({1.0: "a", 2.0: "b"}, [0.01, 0.02])  # type: ignore[arg-type]


def test_factor_ic_report_mapping_factor_raises():
    # A bare dict/Mapping must NOT be silently treated as its keys sequence.
    with pytest.raises(ValueError):
        factor_ic_report({1.0: "a", 2.0: "b"}, [0.01, 0.02], n_quantiles=2)  # type: ignore[arg-type]


def test_factor_ic_report_mapping_forward_returns_raises():
    # forward_returns passed as a dict/Mapping must also be rejected.
    with pytest.raises(ValueError):
        factor_ic_report([1.0, 2.0], {1.0: 0.01, 2.0: 0.02}, n_quantiles=2)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


def test_quantile_bucket_frozen_and_to_dict():
    bucket = QuantileBucket(quantile=1, count=5, mean_return=0.0123)
    assert bucket.to_dict() == {
        "quantile": 1,
        "count": 5,
        "mean_return": 0.0123,
    }
    with pytest.raises(FrozenInstanceError):
        bucket.count = 10  # type: ignore[misc]


def test_factor_ic_result_frozen_and_to_dict():
    result = FactorICResult(
        pearson_ic=0.5,
        spearman_ic=0.4,
        rank_ic=0.4,
        icir=0.6,
        quantile_spread=0.02,
        buckets=[QuantileBucket(quantile=1, count=2, mean_return=0.01)],
        n=4,
    )
    d = result.to_dict()
    assert d["pearson_ic"] == 0.5
    assert d["spearman_ic"] == 0.4
    assert d["rank_ic"] == 0.4
    assert d["icir"] == 0.6
    assert d["quantile_spread"] == 0.02
    assert d["n"] == 4
    assert len(d["buckets"]) == 1
    assert d["buckets"][0]["quantile"] == 1
    with pytest.raises(FrozenInstanceError):
        result.pearson_ic = 0.99  # type: ignore[misc]


# Late import so the module-level import above is the one exercised by RED.
import math  # noqa: E402  (kept at bottom for test-local use of math.isfinite)
