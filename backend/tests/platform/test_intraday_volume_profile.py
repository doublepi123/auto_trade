"""Tests for P381 intraday volume profile.

Validates per-bucket computation, U-shape score, peak/lull times,
and ValueError for invalid inputs.
"""

from __future__ import annotations

import pytest

from app.platform.intraday_volume_profile import (
    IntradayVolumeProfileResult,
    intraday_volume_profile_report,
)


class TestIntradayVolumeProfileReport:
    """Unit tests for intraday_volume_profile_report."""

    def test_typical_u_shape_volumes(self):
        """Construct open/close peak volumes → u_shape_score > 0.3."""
        volumes_by_time = {
            "09:30": [5000, 5200, 5100, 5300, 5150],
            "10:00": [2000, 2100, 2050, 1900, 1950],
            "10:30": [1500, 1400, 1600, 1550, 1450],
            "11:00": [1200, 1300, 1100, 1250, 1150],
            "11:30": [1400, 1350, 1450, 1300, 1400],
            "13:00": [1600, 1550, 1700, 1650, 1500],
            "13:30": [1800, 1900, 1750, 1850, 1700],
            "14:00": [2200, 2100, 2300, 2150, 2250],
            "14:30": [2500, 2600, 2400, 2550, 2450],
            "15:00": [3500, 3400, 3600, 3300, 3700],
            "15:30": [5500, 5600, 5800, 5700, 5400],
        }
        result = intraday_volume_profile_report(volumes_by_time)

        assert isinstance(result, IntradayVolumeProfileResult)
        assert result.u_shape_score > 0.3
        assert len(result.per_bucket) == 11
        for bucket in volumes_by_time:
            assert bucket in result.per_bucket
            assert "avg_volume" in result.per_bucket[bucket]
            assert "pct" in result.per_bucket[bucket]
            assert result.per_bucket[bucket]["avg_volume"] > 0
        assert len(result.peak_times) == 3
        assert len(result.lull_times) == 3
        # peak times should include high-volume buckets (open/close)
        peak_set = set(result.peak_times)
        assert "09:30" in peak_set or "15:30" in peak_set

    def test_flat_volumes_zero_u_shape(self):
        """All buckets equal volume → u_shape_score is symmetric (open+close)/total."""
        volumes_by_time = {
            "09:30": [100, 100, 100],
            "10:00": [100, 100, 100],
            "10:30": [100, 100, 100],
        }
        result = intraday_volume_profile_report(volumes_by_time)
        assert result.u_shape_score == pytest.approx(2.0 / 3.0, 0.01)
        assert result.peak_times == ["09:30", "10:00", "10:30"]

    def test_all_zero_volumes(self):
        """All volumes zero → should not crash, returns zeros."""
        volumes_by_time = {
            "09:30": [0, 0],
            "10:00": [0, 0],
        }
        result = intraday_volume_profile_report(volumes_by_time)
        assert result.u_shape_score == 0.0
        assert result.per_bucket["09:30"]["avg_volume"] == 0.0
        assert result.per_bucket["10:00"]["avg_volume"] == 0.0

    def test_single_bucket(self):
        """Single time bucket. Open and close are the same bucket → score = 2.0."""
        volumes_by_time = {"09:30": [100, 200, 150]}
        result = intraday_volume_profile_report(volumes_by_time)
        # open_pct = 1.0, close_pct = 1.0 → u_shape_score = 2.0
        assert result.u_shape_score == pytest.approx(2.0, 0.01)
        assert result.peak_times == ["09:30"]
        assert result.lull_times == ["09:30"]

    def test_negative_volume_raises(self):
        """Negative volume should raise ValueError."""
        with pytest.raises(ValueError):
            intraday_volume_profile_report({"09:30": [100, -50]})

    def test_non_dict_input_raises(self):
        """Non-dict input raises."""
        with pytest.raises(ValueError):
            intraday_volume_profile_report([1, 2, 3])  # type: ignore[arg-type]

    def test_empty_dict_raises(self):
        """Empty dict raises."""
        with pytest.raises(ValueError):
            intraday_volume_profile_report({})

    def test_empty_volume_list_raises(self):
        """Empty list for a bucket raises."""
        with pytest.raises(ValueError):
            intraday_volume_profile_report({"09:30": []})

    def test_too_many_buckets_raises(self):
        """More than 50 buckets raises."""
        too_many = {f"t{i:02d}:00": [100] for i in range(51)}
        with pytest.raises(ValueError):
            intraday_volume_profile_report(too_many)

    def test_non_numeric_volume_raises(self):
        """Non-numeric entry raises."""
        with pytest.raises(ValueError):
            intraday_volume_profile_report({"09:30": [100, "abc"]})  # type: ignore[list-item]

    def test_to_dict_roundtrip(self):
        """to_dict produces a serialisable dict."""
        volumes_by_time = {"09:30": [100, 200], "10:00": [50, 60]}
        result = intraday_volume_profile_report(volumes_by_time)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "per_bucket" in d
        assert "u_shape_score" in d
        assert "peak_times" in d
        assert "lull_times" in d
        # Verify non-negative pct for non-negative inputs
        for key, val in d["per_bucket"].items():
            assert val["avg_volume"] >= 0
            assert val["pct"] >= 0
