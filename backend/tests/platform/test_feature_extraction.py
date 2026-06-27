"""Tests for P319 feature extraction."""

from __future__ import annotations

import pytest

from app.platform.feature_extraction import feature_extraction_report


class TestFeatureExtractionReport:
    def test_basic_statistics(self):
        """Known series → correct mean and std."""
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = feature_extraction_report(values)
        body = result.to_dict()
        features = body["features"]
        assert features["mean"] == pytest.approx(3.0)
        assert features["std"] == pytest.approx(1.414, abs=1e-3)
        assert features["min"] == 1.0
        assert features["max"] == 5.0
        assert features["range"] == 4.0

    def test_skew_kurtosis(self):
        """Symmetric series: skew ≈ 0; kurtosis computed."""
        values = [-2.0, -1.0, 0.0, 1.0, 2.0]
        result = feature_extraction_report(values)
        features = result.to_dict()["features"]
        assert features["skew"] == pytest.approx(0.0, abs=0.1)
        # Kurtosis for uniform-like is negative (platykurtic)
        assert features["kurt"] < 3.0

    def test_trend_slope(self):
        """Linearly increasing series → positive slope."""
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = feature_extraction_report(values)
        slope = result.to_dict()["features"]["trend_slope"]
        assert slope == pytest.approx(1.0, abs=1e-6)

    def test_autocorr_lag1(self):
        """Repeated values → autocorr ≈ 1.0."""
        values = [5.0] * 10
        result = feature_extraction_report(values)
        assert result.to_dict()["features"]["autocorr_lag1"] == pytest.approx(1.0, abs=0.01)

    def test_max_drawdown(self):
        """Series with a dip → positive drawdown."""
        values = [10.0, 8.0, 12.0, 6.0, 14.0]
        result = feature_extraction_report(values)
        mdd = result.to_dict()["features"]["max_drawdown"]
        assert mdd > 0.0

    def test_rejects_empty_series(self):
        with pytest.raises(ValueError):
            feature_extraction_report([])

    def test_rejects_nan(self):
        with pytest.raises(ValueError):
            feature_extraction_report([1.0, float("nan")])

    def test_rejects_bool(self):
        with pytest.raises(ValueError):
            feature_extraction_report([1.0, True, 3.0])
