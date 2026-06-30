"""Tests for P373 variance ratio test module."""
from __future__ import annotations

import math
import random

import pytest
from app.platform.variance_ratio_test import (
    LagResult,
    VarianceRatioTestResult,
    variance_ratio_test_report,
)


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
