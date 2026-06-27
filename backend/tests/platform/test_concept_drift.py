"""Tests for P317 concept drift detection."""

from __future__ import annotations

import pytest

from app.platform.concept_drift import concept_drift_report


class TestConceptDriftReport:
    def test_detects_mean_shift(self):
        """A mid-series mean jump produces drift_points."""
        # 40 flat values, then 40 values with a clear mean shift
        values = [1.0] * 40 + [5.0] * 40
        result = concept_drift_report(values, window=10, threshold=2.0)
        body = result.to_dict()
        assert len(body["drift_points"]) > 0
        assert len(body["drift_scores"]) == len(values)
        # At least one score should be well above 0 at the shift region
        assert any(s > 1.0 for s in body["drift_scores"])

    def test_no_drift_in_flat_series(self):
        """A flat series has no drift_points."""
        values = [3.0] * 80
        result = concept_drift_report(values, window=10, threshold=2.0)
        body = result.to_dict()
        assert body["drift_points"] == []

    def test_rejects_empty_series(self):
        with pytest.raises(ValueError):
            concept_drift_report([])

    def test_rejects_non_numeric_entries(self):
        with pytest.raises(ValueError):
            concept_drift_report([1.0, float("nan"), 3.0])

    def test_rejects_window_too_small(self):
        with pytest.raises(ValueError):
            concept_drift_report([1.0, 2.0, 3.0], window=1)

    def test_rejects_series_shorter_than_window(self):
        with pytest.raises(ValueError):
            concept_drift_report([1.0, 2.0, 3.0], window=5)
