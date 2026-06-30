"""Tests for P384 higher moment forecast.

Validates rolling skewness/kurtosis AR(1) forecast, persistence coefficients,
and predictability flag. Asserts skewness_persistence >= 0 for clustered returns.
"""

from __future__ import annotations

import math
import random

import pytest

from app.platform.higher_moment_forecast import (
    HigherMomentForecastResult,
    higher_moment_forecast_report,
)


def _volatility_clustered_returns(
    n: int, seed: int = 42
) -> list[float]:
    """Generate returns with volatility clustering (GARCH-like).

    High-volatility clusters produce skewed/kurtic distributions at certain
    windows, creating persistence in higher moments.
    """
    rng = random.Random(seed)
    returns: list[float] = []
    sigma = 0.01
    for i in range(n):
        # GARCH(1,1)-like volatility
        sigma = math.sqrt(
            0.000001 + 0.85 * sigma**2 + 0.1 * (returns[-1]**2 if returns else 0.0)
        )
        ret = rng.gauss(0, sigma)
        returns.append(ret)
    return returns


class TestHigherMomentForecastReport:
    """Unit tests for higher_moment_forecast_report."""

    def test_clustered_returns_skewness_persistence_non_negative(self):
        """Volatility-clustered returns → skewness_persistence >= 0."""
        returns = _volatility_clustered_returns(500)
        result = higher_moment_forecast_report(returns, window=20)

        assert isinstance(result, HigherMomentForecastResult)
        assert result.skewness_persistence >= 0.0
        assert isinstance(result.is_skewness_predictable, bool)

    def test_kurtosis_forecast_is_finite(self):
        """Kurtosis forecast should be a finite number."""
        returns = _volatility_clustered_returns(300)
        result = higher_moment_forecast_report(returns, window=15)

        assert math.isfinite(result.kurtosis_forecast)
        assert math.isfinite(result.skewness_forecast)
        assert math.isfinite(result.kurtosis_persistence)

    def test_symmetric_returns_low_skewness(self):
        """Symmetric Gaussian returns → skewness near 0."""
        rng = random.Random(123)
        returns = [rng.gauss(0, 0.01) for _ in range(200)]
        result = higher_moment_forecast_report(returns, window=20)

        # Skewness forecast should be near 0
        assert abs(result.skewness_forecast) < 1.0

    def test_default_window_and_horizon(self):
        """Default parameters should work."""
        returns = _volatility_clustered_returns(100)
        result = higher_moment_forecast_report(returns)

        assert isinstance(result, HigherMomentForecastResult)
        assert isinstance(result.is_skewness_predictable, bool)

    def test_custom_horizon(self):
        """Custom forecast horizon accepted."""
        returns = _volatility_clustered_returns(200)
        result = higher_moment_forecast_report(
            returns, window=20, forecast_horizon=1
        )
        assert isinstance(result, HigherMomentForecastResult)

    def test_window_too_small_raises(self):
        """Window smaller than min raises."""
        with pytest.raises(ValueError, match="window must be at least"):
            higher_moment_forecast_report(
                [0.01, 0.02, 0.03, 0.04], window=2
            )

    def test_not_enough_returns_raises(self):
        """Returns shorter than window raises."""
        with pytest.raises(ValueError):
            higher_moment_forecast_report(
                [0.01, 0.02, 0.03], window=5
            )

    def test_empty_returns_raises(self):
        """Empty list raises."""
        with pytest.raises(ValueError, match="non-empty"):
            higher_moment_forecast_report([])

    def test_non_list_returns_raises(self):
        """Non-list raises."""
        with pytest.raises(ValueError):
            higher_moment_forecast_report("not a list")  # type: ignore[arg-type]

    def test_non_numeric_raises(self):
        """Non-numeric entries raise."""
        with pytest.raises(ValueError):
            higher_moment_forecast_report([0.01, "bad"], window=5)  # type: ignore[list-item]

    def test_negative_window_raises(self):
        """Negative window raises."""
        with pytest.raises(ValueError):
            higher_moment_forecast_report(
                [0.01] * 50, window=-1  # type: ignore[arg-type]
            )

    def test_zero_horizon_raises(self):
        """Zero forecast horizon raises."""
        with pytest.raises(ValueError):
            higher_moment_forecast_report(
                [0.01] * 50, window=5, forecast_horizon=0  # type: ignore[arg-type]
            )

    def test_to_dict_roundtrip(self):
        """to_dict contains expected keys."""
        returns = _volatility_clustered_returns(100)
        result = higher_moment_forecast_report(returns, window=10)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "skewness_forecast" in d
        assert "kurtosis_forecast" in d
        assert "skewness_persistence" in d
        assert "kurtosis_persistence" in d
        assert "is_skewness_predictable" in d
        assert isinstance(d["is_skewness_predictable"], bool)

    def test_very_few_valid_windows(self):
        """Edge case: barely enough data after window."""
        returns = [0.01] * 6  # window=5 gives 2 moment values
        result = higher_moment_forecast_report(returns, window=5)
        assert isinstance(result, HigherMomentForecastResult)
        assert math.isfinite(result.skewness_forecast)
