"""Tests for P365 variance-break (ICSS algorithm) module."""
from __future__ import annotations

import pytest
from app.platform.variance_break import (
    VarianceBreakResult,
    variance_break_report,
)


class TestVarianceBreakReport:
    def test_clean_series_no_break(self) -> None:
        """A series with constant variance should yield no break points."""
        returns = [0.001] * 100
        result = variance_break_report(returns, min_segment=10)
        assert isinstance(result, VarianceBreakResult)
        assert result.break_points == []
        assert result.variance_ratios == []
        assert result.icss_statistics == []

    def test_variance_shift_detected(self) -> None:
        """Half low-variance, half high-variance should detect at least one break."""
        low_var = [0.0005 * (1.0 if i % 2 == 0 else -1.0) for i in range(100)]
        high_var = [0.005 * (1.0 if i % 2 == 0 else -1.0) for i in range(100)]
        returns = low_var + high_var
        result = variance_break_report(returns, min_segment=10)
        assert isinstance(result, VarianceBreakResult)
        assert len(result.break_points) >= 1
        # The break should be near index 100
        bp = result.break_points[0]
        assert 80 <= bp <= 120

    def test_variance_ratios_positive(self) -> None:
        """variance_ratios should be positive for each break point."""
        low_var = [0.0001] * 50
        high_var = [0.01 * (1.0 if i % 2 == 0 else -1.0) for i in range(50)]
        returns = low_var + high_var
        result = variance_break_report(returns, min_segment=10)
        if result.break_points:
            assert len(result.variance_ratios) == len(result.break_points)
            for ratio in result.variance_ratios:
                assert ratio > 0.0

    def test_icss_statistics_non_negative(self) -> None:
        """ICSS statistics should be non-negative (absolute values)."""
        low_var = [0.0001] * 50
        high_var = [0.01 * (1.0 if i % 2 == 0 else -1.0) for i in range(50)]
        returns = low_var + high_var
        result = variance_break_report(returns, min_segment=10)
        if result.break_points:
            assert len(result.icss_statistics) == len(result.break_points)
            for stat in result.icss_statistics:
                assert stat >= 0.0

    def test_pre_post_stats_has_segments(self) -> None:
        """pre_post_stats should contain entries for each segment."""
        low_var = [0.0001] * 30
        high_var = [0.01 * (1.0 if i % 2 == 0 else -1.0) for i in range(30)]
        returns = low_var + high_var
        result = variance_break_report(returns, min_segment=10)
        if result.break_points:
            assert len(result.pre_post_stats) >= 2
            for seg_key, seg_data in result.pre_post_stats.items():
                assert "start" in seg_data
                assert "end" in seg_data
                assert "variance" in seg_data
                assert seg_data["variance"] >= 0.0

    def test_to_dict(self) -> None:
        """to_dict returns a dict with all fields."""
        returns = [0.001] * 100
        result = variance_break_report(returns, min_segment=10)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "break_points" in d
        assert "variance_ratios" in d
        assert "icss_statistics" in d
        assert "pre_post_stats" in d
        assert "n_observations" in d

    def test_empty_returns_raises(self) -> None:
        """Empty return list should raise ValueError."""
        with pytest.raises(ValueError):
            variance_break_report([], min_segment=10)

    def test_too_short_returns_raises(self) -> None:
        """Returns shorter than 2*min_segment should raise ValueError."""
        with pytest.raises(ValueError):
            variance_break_report([0.001] * 5, min_segment=10)

    def test_non_finite_values_raise(self) -> None:
        """Non-finite values should raise ValueError."""
        with pytest.raises(ValueError):
            variance_break_report([float("nan"), 0.001, 0.002])

    def test_three_regime_variance_shift(self) -> None:
        """Three variance regimes should detect multiple break points."""
        low_var = [0.0001] * 40
        mid_var = [0.002 * (1.0 if i % 2 == 0 else -1.0) for i in range(40)]
        high_var = [0.02 * (1.0 if i % 2 == 0 else -1.0) for i in range(40)]
        returns = low_var + mid_var + high_var
        result = variance_break_report(returns, min_segment=10)
        assert len(result.break_points) >= 2
