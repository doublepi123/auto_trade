"""P385: BDS test for independence — unit tests."""

from __future__ import annotations

import math
import random

import pytest

from app.platform.bds_test import bds_test_report


class TestBdsTest:
    """BDS test unit tests."""

    def test_iid_normal_likely_independent(self) -> None:
        """i.i.d. normal sequence should likely be judged independent."""
        rng = random.Random(42)
        series = [rng.gauss(0, 1) for _ in range(500)]
        result = bds_test_report(series)
        # BDS statistic should not be extreme for i.i.d. data
        # Check that the result has expected structure
        assert isinstance(result.bds_statistic, float)
        assert math.isfinite(result.bds_statistic)
        assert math.isfinite(result.p_value)
        assert 0.0 <= result.p_value <= 1.0
        # For i.i.d. Normal, we likely fail to reject independence
        assert result.is_independent or abs(result.bds_statistic) < 3.0

    def test_strong_autocorrelation_detected(self) -> None:
        """Strongly autocorrelated series should give large |BDS|."""
        rng = random.Random(42)
        series: list[float] = [0.0]
        for _ in range(499):
            series.append(0.9 * series[-1] + rng.gauss(0, 0.1))
        result = bds_test_report(series)
        assert abs(result.bds_statistic) > 2.0
        assert result.is_independent is False or result.p_value < 0.05

    def test_default_epsilon_and_dimension(self) -> None:
        """Default epsilon = 0.7 * std, default m = 2."""
        rng = random.Random(42)
        series = [rng.gauss(0, 1) for _ in range(200)]
        result = bds_test_report(series)
        assert result.correlation_integral_1 >= 0
        assert result.correlation_integral_m >= 0

    def test_custom_embedding_dimension(self) -> None:
        """Custom embedding dimension m = 3."""
        rng = random.Random(42)
        series = [rng.gauss(0, 1) for _ in range(200)]
        result = bds_test_report(series, embedding_dimension=3)
        assert result.correlation_integral_m >= 0

    def test_custom_epsilon(self) -> None:
        """Custom epsilon value."""
        rng = random.Random(42)
        series = [rng.gauss(0, 1) for _ in range(200)]
        result = bds_test_report(series, epsilon=0.5)
        assert math.isfinite(result.bds_statistic)

    def test_constant_series_independent(self) -> None:
        """Constant series should be treated as independent (C_m = C_1^m = 1)."""
        series = [1.0] * 100
        result = bds_test_report(series)
        assert result.is_independent
        assert result.p_value == pytest.approx(1.0, abs=1e-6)

    def test_to_dict_all_keys(self) -> None:
        """to_dict returns all expected keys."""
        rng = random.Random(42)
        series = [rng.gauss(0, 1) for _ in range(100)]
        result = bds_test_report(series)
        d = result.to_dict()
        for key in (
            "bds_statistic",
            "correlation_integral_m",
            "correlation_integral_1",
            "p_value",
            "is_independent",
        ):
            assert key in d

    def test_short_series_raises(self) -> None:
        """Too-short series raises ValueError."""
        with pytest.raises(ValueError):
            bds_test_report([0.1, 0.2, 0.3])

    def test_embedding_dimension_too_low_raises(self) -> None:
        """embedding_dimension < 2 raises ValueError."""
        rng = random.Random(42)
        series = [rng.gauss(0, 1) for _ in range(50)]
        with pytest.raises(ValueError):
            bds_test_report(series, embedding_dimension=1)

    def test_embedding_dimension_exceeds_length_raises(self) -> None:
        """embedding_dimension > n raises ValueError."""
        series = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        with pytest.raises(ValueError):
            bds_test_report(series, embedding_dimension=10)
