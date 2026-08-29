"""Tests for P373 variance ratio test module."""
from __future__ import annotations

import inspect
import math
import random
from typing import Protocol, runtime_checkable

import pytest
from app.platform.variance_ratio_test import (
    LagResult,
    VarianceRatioTestResult,
    variance_ratio_test_report,
)


@runtime_checkable
class CorrectedLagResult(Protocol):
    @property
    def z_robust(self) -> float: ...

    @property
    def p_one_sided_lower(self) -> float: ...


@runtime_checkable
class ValidatedVarianceRatioResult(Protocol):
    @property
    def valid(self) -> bool: ...

    @property
    def invalid_reason(self) -> str | None: ...


class TestVarianceRatioTestReport:
    """Tests for variance_ratio_test_report function."""

    def test_random_walk_prices_vr_near_one(self) -> None:
        """For a random walk, VR should be close to 1."""
        random.seed(42)
        # Generate a random walk: p_{t} = p_{t-1} + eps_t, eps_t ~ N(0, 0.01)
        prices = [100.0]
        for _ in range(200):
            eps = random.gauss(0.0, 0.01)
            prices.append(prices[-1] * math.exp(eps))
        result = variance_ratio_test_report(prices, lags=[2, 5, 10])
        assert isinstance(result, VarianceRatioTestResult)
        assert result.n_observations == 201
        # VR should be close to 1 for a random walk
        for lr in result.per_lag:
            assert abs(lr.vr - 1.0) < 0.5
            assert isinstance(lr.z_stat, float)
            assert isinstance(lr.p_value, float)
        # Most likely a random walk
        assert result.is_random_walk is True

    def test_trending_prices_vr_above_one(self) -> None:
        """For a trending series, VR should be > 1 (positive autocorrelation)."""
        # Linear trend + tiny noise
        prices = [100.0 + i * 0.1 + random.gauss(0.0, 0.001) for i in range(100)]
        result = variance_ratio_test_report(prices, lags=[2, 5, 10])
        # At least one VR should deviate from 1
        has_deviation = False
        for lr in result.per_lag:
            if abs(lr.vr - 1.0) > 0.02:
                has_deviation = True
        assert has_deviation

    def test_default_lags_produces_result(self) -> None:
        """Default lags parameter should work."""
        prices = [100.0]
        for _ in range(50):
            prices.append(prices[-1] * (1.0 + random.gauss(0.0, 0.01)))
        result = variance_ratio_test_report(prices)
        assert len(result.per_lag) >= 1

    def test_per_lag_is_list_of_lag_result(self) -> None:
        """Each item in per_lag should be a LagResult."""
        prices = [100.0 + i * 0.1 for i in range(30)]
        result = variance_ratio_test_report(prices, lags=[2, 5])
        for lr in result.per_lag:
            assert isinstance(lr, LagResult)
            assert isinstance(lr.lag, int)
            assert isinstance(lr.vr, float)
            assert isinstance(lr.z_stat, float)
            assert isinstance(lr.p_value, float)

    def test_invalid_prices_raises(self) -> None:
        """Invalid prices should raise ValueError."""
        with pytest.raises(ValueError):
            variance_ratio_test_report([])
        with pytest.raises(ValueError):
            variance_ratio_test_report([100.0, 101.0])
        with pytest.raises(ValueError):
            variance_ratio_test_report([100.0, -101.0, 102.0])

    def test_invalid_lags_raises(self) -> None:
        """Invalid lags should raise ValueError."""
        prices = [100.0 + i * 0.5 for i in range(20)]
        with pytest.raises(ValueError, match="must be an int"):
            variance_ratio_test_report(prices, lags=[True])

    def test_lags_too_large_skipped(self) -> None:
        """Lags larger than series length should be skipped gracefully."""
        prices = [100.0 + i for i in range(10)]
        result = variance_ratio_test_report(prices, lags=[2, 5, 50])
        # Only lags < len(prices) should be included
        for lr in result.per_lag:
            assert lr.lag < 10

    def test_flat_prices(self) -> None:
        """Flat prices (constant) should produce VR≈1 with zero variance returns."""
        prices = [100.0] * 30
        result = variance_ratio_test_report(prices, lags=[2, 5])
        assert isinstance(result, VarianceRatioTestResult)

    def test_to_dict_serializable(self) -> None:
        """to_dict should produce a JSON-serializable dictionary."""
        prices = [100.0 + i * 0.5 + random.gauss(0.0, 0.1) for i in range(50)]
        result = variance_ratio_test_report(prices, lags=[2, 5])
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "per_lag" in d
        assert "is_random_walk" in d
        assert "n_observations" in d
        for lr_dict in d["per_lag"]:
            assert "lag" in lr_dict
            assert "vr" in lr_dict
            assert "z_stat" in lr_dict
            assert "p_value" in lr_dict


def _standard_normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _seeded_heteroskedastic_prices() -> list[float]:
    # arch 8.0.0 reference fixture: rng = random.Random(20260829);
    # returns = [rng.gauss(0.0002, 0.002 if i < 200 else 0.02)
    # for i in range(400)]; prices start at 100 and compound exp(return).
    rng = random.Random(20260829)
    returns = [
        rng.gauss(0.0002, 0.002 if index < 200 else 0.02)
        for index in range(400)
    ]
    prices = [100.0]
    for value in returns:
        prices.append(prices[-1] * math.exp(value))
    return prices


def _seeded_ar_prices(coefficient: float) -> list[float]:
    rng = random.Random(20260830)
    returns = [rng.gauss(0.0, 0.01)]
    for _ in range(298):
        returns.append(coefficient * returns[-1] + rng.gauss(0.0, 0.01))
    prices = [100.0]
    for value in returns:
        prices.append(prices[-1] * math.exp(value))
    return prices


class TestLoMacKinlayCorrections:
    """Pins the debiased and heteroskedasticity-robust Lo-MacKinlay test."""

    def test_robust_statistic_does_not_overstate_heteroskedastic_series(self) -> None:
        result = variance_ratio_test_report(
            _seeded_heteroskedastic_prices(), lags=[5]
        )

        lag_result = result.per_lag[0]

        assert isinstance(lag_result, CorrectedLagResult)
        assert abs(lag_result.z_robust) <= abs(lag_result.z_stat)
        assert abs(lag_result.z_robust - lag_result.z_stat) > 0.1

    def test_robust_statistic_matches_arch_reference(self) -> None:
        result = variance_ratio_test_report(
            _seeded_heteroskedastic_prices(), lags=[5]
        )

        lag_result = result.per_lag[0]

        assert isinstance(lag_result, CorrectedLagResult)
        # arch 8.0.0: VarianceRatio([log(p) for p in prices], lags=5,
        # trend="c", debiased=True, robust=True, overlap=True).stat.
        # abs=1e-6 permits only cross-library floating-point accumulation noise.
        assert lag_result.z_robust == pytest.approx(-0.6897530032989094, abs=1e-6)

    def test_variance_ratio_uses_lo_macKinlay_m_correction(self) -> None:
        result = variance_ratio_test_report(
            _seeded_heteroskedastic_prices(), lags=[5]
        )

        lag_result = result.per_lag[0]

        # arch 8.0.0, using the same call as above: .vr. The repository consumes
        # prices, so log-price levels are passed to arch to match its differencing.
        assert lag_result.vr == pytest.approx(0.8940692345749837, abs=1e-6)
        assert lag_result.vr != pytest.approx(0.8850458829374356, abs=1e-6)


class TestOneSidedLowerTail:
    """The lower tail must come from the SAME statistic a gate would act on.

    ``p_value`` stays the homoskedastic two-sided number for endpoint
    continuity, so ``p_one_sided_lower`` is NOT half of it -- the two are
    computed from different z statistics. Pinning them to a 2x relationship
    would force the robust tail to be derived from the homoskedastic z, which
    is exactly the overstatement this fix removes. It is pinned against
    Phi(z_robust) instead.
    """

    def test_negative_robust_statistic_gives_its_own_lower_tail(self) -> None:
        result = variance_ratio_test_report(_seeded_ar_prices(-0.55), lags=[5])

        lag_result = result.per_lag[0]

        assert isinstance(lag_result, CorrectedLagResult)
        assert lag_result.z_robust < 0.0
        assert lag_result.p_one_sided_lower == pytest.approx(
            _standard_normal_cdf(lag_result.z_robust), abs=1e-9
        )
        assert lag_result.p_one_sided_lower < 0.5

    def test_lower_tail_is_not_derived_from_the_homoskedastic_statistic(
        self,
    ) -> None:
        """Guards the defect directly: a robust tail read off the homoskedastic
        z would understate the p-value whenever the series is heteroskedastic."""
        result = variance_ratio_test_report(_seeded_heteroskedastic_prices(), lags=[5])

        lag_result = result.per_lag[0]

        assert isinstance(lag_result, CorrectedLagResult)
        assert lag_result.p_one_sided_lower == pytest.approx(
            _standard_normal_cdf(lag_result.z_robust), abs=1e-9
        )
        assert lag_result.p_one_sided_lower != pytest.approx(
            lag_result.p_value / 2.0, abs=1e-6
        )

    def test_positive_robust_statistic_has_lower_tail_above_half(self) -> None:
        result = variance_ratio_test_report(_seeded_ar_prices(0.55), lags=[5])

        lag_result = result.per_lag[0]

        assert isinstance(lag_result, CorrectedLagResult)
        assert lag_result.z_robust > 0.0
        assert lag_result.p_one_sided_lower > 0.5


class TestStatisticalValidityFloor:
    """Lag 2 is rejected per lag, so another admissible requested lag still reports."""

    def test_validity_floor_defaults_are_public_api(self) -> None:
        parameters = inspect.signature(variance_ratio_test_report).parameters

        assert parameters["min_observations"].default == 200
        assert parameters["min_n_over_q"].default == 10

    def test_series_below_observation_floor_refuses_opinion(self) -> None:
        prices = [100.0 * math.exp(index * 0.001) for index in range(150)]

        result = variance_ratio_test_report(prices, lags=[5])

        assert isinstance(result, ValidatedVarianceRatioResult)
        assert result.valid is False
        assert result.invalid_reason is not None
        assert "observation" in result.invalid_reason.lower()
        assert result.is_random_walk is False

    def test_lag_below_n_over_q_floor_is_rejected(self) -> None:
        prices = [100.0 * math.exp(index * 0.001) for index in range(300)]

        result = variance_ratio_test_report(prices, lags=[100])

        assert isinstance(result, ValidatedVarianceRatioResult)
        assert result.valid is False
        assert result.invalid_reason is not None
        assert "n/q" in result.invalid_reason.lower()
        assert result.is_random_walk is False

    def test_lag_two_is_rejected_for_bid_ask_bounce(self) -> None:
        prices = [100.0 * math.exp(index * 0.001) for index in range(300)]

        result = variance_ratio_test_report(prices, lags=[2])

        assert isinstance(result, ValidatedVarianceRatioResult)
        assert result.valid is False
        assert result.invalid_reason is not None
        # VR(2) is mechanically depressed by Roll bid-ask bounce on trade prices.
        assert "bid-ask bounce" in result.invalid_reason.lower()
        assert result.is_random_walk is False

    def test_rejected_lag_two_does_not_discard_admissible_lag(self) -> None:
        prices = _seeded_ar_prices(0.2)

        result = variance_ratio_test_report(prices, lags=[2, 5])

        assert isinstance(result, ValidatedVarianceRatioResult)
        assert result.valid is True
        assert [lag_result.lag for lag_result in result.per_lag] == [5]
        assert result.invalid_reason is not None
        assert "bid-ask bounce" in result.invalid_reason.lower()

    def test_sufficient_series_and_lag_are_valid(self) -> None:
        prices = _seeded_ar_prices(0.2)

        result = variance_ratio_test_report(prices, lags=[5])

        assert isinstance(result, ValidatedVarianceRatioResult)
        assert result.valid is True
        assert result.invalid_reason is None

    def test_added_statistics_and_validity_are_serialized(self) -> None:
        result = variance_ratio_test_report(_seeded_ar_prices(0.2), lags=[5])

        serialized = result.to_dict()

        assert "valid" in serialized
        assert "invalid_reason" in serialized
        assert "z_robust" in serialized["per_lag"][0]
        assert "p_one_sided_lower" in serialized["per_lag"][0]


class TestVarianceRatioDocumentation:
    def test_robust_claim_requires_public_robust_statistic(self) -> None:
        module = inspect.getmodule(LagResult)
        assert module is not None
        module_docstring = module.__doc__ or ""

        robust_statistic_is_public = "z_robust" in LagResult.__dataclass_fields__

        assert (
            "heteroskedasticity-robust" not in module_docstring
            or robust_statistic_is_public
        )
