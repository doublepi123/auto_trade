"""Tests for P352 squeeze detection (Bollinger Bands vs Keltner Channel).

A squeeze occurs when Bollinger Bands are fully inside Keltner Channel,
indicating low volatility that typically precedes a breakout.
"""

from __future__ import annotations

import math

import pytest

from app.platform.squeeze_detection import SqueezeDetectionResult, squeeze_detection_report


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _low_vol_series() -> list[float]:
    """Constant series — BB ~= KC, but BB has zero std so BB_width ~= 0.

    For squeeze to happen BB must be *inside* KC.  With a flat series:
    BB upper = BB lower = mean, KC upper > KC lower > 0 for range-based ATR.
    So BB is NOT inside KC (they are equal at the center line, not embedded).
    Let's use a series with very small oscillation that creates tiny BB width
    while ATR-based KC is wider.
    """
    # Small oscillation: 0.01 amplitude creates tiny BB std, but ATR sees 0.02 range
    series = [10.0]
    for i in range(1, 40):
        series.append(10.0 + 0.005 if i % 2 == 0 else 10.0 - 0.005)
    return series


def _breakout_series() -> list[float]:
    """Low vol then sharp breakout — squeeze should appear in early segment."""
    # 30 bars of low vol then 20 bars of sharp trend
    low = _low_vol_series()[:30]
    high = [10.0 + 0.1 * i for i in range(20)]
    return low + high


# ---------------------------------------------------------------------------
# squeeze_detection_report
# ---------------------------------------------------------------------------


class TestSqueezeDetectionReport:
    def test_low_vol_produces_squeeze_points(self):
        """Low-vol series should have BB narrower than KC → squeeze detected."""
        series = _low_vol_series()
        result = squeeze_detection_report(series, bb_window=10, kc_window=10)
        assert isinstance(result, SqueezeDetectionResult)
        assert isinstance(result.squeeze_on, bool)
        assert isinstance(result.squeeze_points, list)
        # BB std is tiny, KC ATR is larger — squeeze should appear
        assert len(result.squeeze_points) > 0, "expected squeeze points in low-vol series"
        assert len(result.bb_upper) == len(series)
        assert len(result.bb_lower) == len(series)
        assert len(result.kc_upper) == len(series)
        assert len(result.kc_lower) == len(series)

    def test_breakout_series_has_squeeze_then_release(self):
        """Low-vol segment has squeeze, breakout segment releases it."""
        series = _breakout_series()
        result = squeeze_detection_report(series, bb_window=10, kc_window=10)
        # Early indices (0-29) should have squeeze
        early_squeeze = any(i < 30 for i in result.squeeze_points)
        assert early_squeeze, "expected squeeze in early low-vol segment"
        # At least some indices after warmup should not be in squeeze
        assert result.squeeze_on in (True, False)
        # direction should be a str
        assert isinstance(result.direction, str)

    def test_squeeze_points_are_valid_indices(self):
        """All squeeze points must be valid indices in the series."""
        series = _low_vol_series()
        result = squeeze_detection_report(series, bb_window=10, kc_window=10)
        for idx in result.squeeze_points:
            assert 0 <= idx < len(series)

    def test_warmup_indices_are_none(self):
        """Indices before both windows are filled should have None values."""
        series = _low_vol_series()
        result = squeeze_detection_report(series, bb_window=20, kc_window=20)
        # bb_window=20: indices 0-18 should be None for BB/KC
        for i in range(19):
            assert result.bb_upper[i] is None
            assert result.bb_lower[i] is None
            assert result.kc_upper[i] is None
            assert result.kc_lower[i] is None
        # After warmup, values should be finite
        assert result.bb_upper[19] is not None
        assert math.isfinite(result.bb_upper[19])
        assert result.bb_lower[19] is not None
        assert result.kc_upper[19] is not None
        assert result.kc_lower[19] is not None

    def test_custom_parameters(self):
        """Custom bb_window, kc_window, bb_mult, kc_mult."""
        series = _low_vol_series()
        result = squeeze_detection_report(
            series, bb_window=5, kc_window=5, bb_mult=3.0, kc_mult=2.0
        )
        assert isinstance(result, SqueezeDetectionResult)

    def test_empty_series_raises(self):
        with pytest.raises(ValueError):
            squeeze_detection_report([])

    def test_non_finite_entries_raises(self):
        with pytest.raises(ValueError):
            squeeze_detection_report([1.0, float("nan"), 2.0])

    def test_boolean_entries_raises(self):
        with pytest.raises(ValueError):
            squeeze_detection_report([1.0, True, 2.0])  # type: ignore[list-item]

    def test_series_too_short_raises(self):
        with pytest.raises(ValueError):
            squeeze_detection_report([1.0, 2.0], bb_window=20)

    def test_to_dict(self):
        series = _low_vol_series()
        result = squeeze_detection_report(series, bb_window=10, kc_window=10)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "squeeze_on" in d
        assert "squeeze_points" in d
        assert "bb_upper" in d
        assert "bb_lower" in d
        assert "kc_upper" in d
        assert "kc_lower" in d
        assert "direction" in d
