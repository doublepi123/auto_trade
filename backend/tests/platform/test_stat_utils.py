"""Tests for shared pure-Python statistical primitives."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

from app.platform.stat_utils import (
    betainc,
    kurtosis,
    percentile,
    quantile,
    skewness,
    standard_error_of_mean,
    summary_report,
    t_cdf,
)


def test_quantile_uses_numpy_linear_interpolation():
    # Given
    values = list(range(1, 101))

    # When / Then
    assert quantile(values, 0.0) == pytest.approx(1.0)
    assert quantile(values, 0.25) == pytest.approx(25.75)
    assert quantile(values, 0.5) == pytest.approx(50.5)
    assert quantile(values, 0.75) == pytest.approx(75.25)
    assert quantile(values, 1.0) == pytest.approx(100.0)


def test_quantile_single_value_is_that_value():
    assert quantile([7.5], 0.37) == pytest.approx(7.5)


def test_quantile_rejects_empty_values_and_invalid_probability():
    with pytest.raises(ValueError):
        quantile([], 0.5)
    with pytest.raises(ValueError):
        quantile([1.0], -0.01)
    with pytest.raises(ValueError):
        quantile([1.0], 1.01)


def test_percentile_scales_percent_to_quantile():
    values = list(range(1, 101))

    assert percentile(values, 25.0) == pytest.approx(25.75)
    assert percentile(values, 50.0) == pytest.approx(50.5)


def test_percentile_rejects_invalid_percent():
    with pytest.raises(ValueError):
        percentile([1.0], -1.0)
    with pytest.raises(ValueError):
        percentile([1.0], 101.0)


def test_betainc_endpoints_and_known_beta_cdf_value():
    assert betainc(2.0, 3.0, 0.0) == 0.0
    assert betainc(2.0, 3.0, 1.0) == 1.0
    assert betainc(2.0, 3.0, 0.5) == pytest.approx(0.6875, abs=1e-12)


def test_betainc_uses_symmetry_relation():
    direct = betainc(2.0, 5.0, 0.25)
    reflected = 1.0 - betainc(5.0, 2.0, 0.75)

    assert direct == pytest.approx(reflected, abs=1e-12)


def test_betainc_rejects_invalid_parameters():
    with pytest.raises(ValueError):
        betainc(0.0, 1.0, 0.5)
    with pytest.raises(ValueError):
        betainc(1.0, -1.0, 0.5)
    with pytest.raises(ValueError):
        betainc(1.0, 1.0, 1.1)


@pytest.mark.parametrize("df", [1.0, 5.0, 30.0])
def test_t_cdf_at_zero_is_half(df: float):
    assert t_cdf(0.0, df) == pytest.approx(0.5)


def test_t_cdf_matches_cauchy_known_value_and_symmetry():
    assert t_cdf(1.0, 1.0) == pytest.approx(0.75, abs=1e-12)
    assert t_cdf(-1.5, 7.0) == pytest.approx(
        1.0 - t_cdf(1.5, 7.0),
        abs=1e-12,
    )


def test_t_cdf_large_magnitudes_approach_limits():
    assert t_cdf(100.0, 10.0) == pytest.approx(1.0, abs=1e-10)
    assert t_cdf(-100.0, 10.0) == pytest.approx(0.0, abs=1e-10)


def test_t_cdf_rejects_invalid_degrees_of_freedom():
    with pytest.raises(ValueError):
        t_cdf(0.0, 0.0)
    with pytest.raises(ValueError):
        t_cdf(0.0, -1.0)
    with pytest.raises(ValueError):
        t_cdf(math.nan, 3.0)


def test_skewness_for_symmetric_and_asymmetric_series():
    assert skewness([-2.0, -1.0, 0.0, 1.0, 2.0]) == pytest.approx(
        0.0,
        abs=1e-12,
    )
    assert skewness([1.0, 1.0, 2.0]) == pytest.approx(
        1.0 / math.sqrt(2.0),
        abs=1e-12,
    )


def test_kurtosis_is_fisher_excess_kurtosis():
    mesokurtic = [-1.0, 0.0, 0.0, 0.0, 0.0, 1.0]

    assert kurtosis(mesokurtic) == pytest.approx(0.0, abs=1e-12)
    assert kurtosis([-1.0, 1.0]) == pytest.approx(-2.0, abs=1e-12)


def test_shape_statistics_return_zero_for_single_or_constant_series():
    assert skewness([4.0]) == 0.0
    assert kurtosis([4.0]) == 0.0
    assert skewness([4.0, 4.0, 4.0]) == 0.0
    assert kurtosis([4.0, 4.0, 4.0]) == 0.0


def test_standard_error_uses_sample_standard_deviation():
    assert standard_error_of_mean([1.0, 2.0, 3.0, 4.0]) == pytest.approx(
        math.sqrt(5.0 / 12.0),
        abs=1e-12,
    )
    assert standard_error_of_mean([3.0]) == 0.0


@pytest.mark.parametrize(
    "function",
    [skewness, kurtosis, standard_error_of_mean, summary_report],
)
def test_series_statistics_reject_empty_values(function):
    with pytest.raises(ValueError):
        function([])


def test_series_statistics_reject_non_finite_values():
    with pytest.raises(ValueError):
        summary_report([1.0, math.nan])
    with pytest.raises(ValueError):
        quantile([1.0, math.inf], 0.5)


def test_summary_report_aggregates_statistics_and_is_frozen():
    report = summary_report([1.0, 2.0, 3.0, 4.0, 5.0])
    body = report.to_dict()

    assert body["n"] == 5
    assert body["mean"] == pytest.approx(3.0)
    assert body["std"] == pytest.approx(math.sqrt(2.5), abs=1e-12)
    assert body["skew"] == pytest.approx(0.0, abs=1e-12)
    assert body["kurtosis"] == pytest.approx(-1.3, abs=1e-12)
    assert body["sem"] == pytest.approx(math.sqrt(0.5), abs=1e-12)
    assert body["median"] == pytest.approx(3.0)
    with pytest.raises(FrozenInstanceError):
        setattr(report, "mean", 0.0)
