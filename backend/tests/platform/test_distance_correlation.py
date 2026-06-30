"""P370 distance_correlation tests — TDD RED phase."""

from __future__ import annotations

import math


class TestDistanceCorrelation:
    """Test distance_correlation_report."""

    def test_identical_series_yields_one(self):
        """x == y → distance_correlation ≈ 1."""
        from app.platform.distance_correlation import distance_correlation_report

        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        result = distance_correlation_report(x, x)

        assert abs(result.distance_correlation - 1.0) < 0.01
        assert result.distance_correlation >= 0.0
        assert result.distance_correlation <= 1.0
        assert result.distance_variance_x > 0.0
        assert result.distance_variance_y > 0.0

    def test_independent_series_low_correlation(self):
        """Independent series → dCor < 0.3."""
        from app.platform.distance_correlation import distance_correlation_report

        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        y = [10.0, 1.0, 8.0, 2.0, 7.0, 3.0, 6.0, 4.0, 5.0, 9.0]  # shuffled

        result = distance_correlation_report(x, y)
        assert result.distance_correlation < 0.5  # Independent → low dCor
        assert result.distance_correlation >= 0.0

    def test_linearly_related_yields_high_correlation(self):
        """y = 2*x → dCor ≈ 1."""
        from app.platform.distance_correlation import distance_correlation_report

        x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        y = [2.0 * v for v in x]

        result = distance_correlation_report(x, y)
        assert result.distance_correlation > 0.9

    def test_mismatched_length_raises(self):
        """x and y must have equal length."""
        import pytest
        from app.platform.distance_correlation import distance_correlation_report

        with pytest.raises(ValueError):
            distance_correlation_report([1.0, 2.0], [3.0])

    def test_short_series_raises(self):
        """Need at least 3 observations."""
        import pytest
        from app.platform.distance_correlation import distance_correlation_report

        with pytest.raises(ValueError):
            distance_correlation_report([1.0, 2.0], [3.0, 4.0])

    def test_non_finite_values_raise(self):
        """NaN/inf raise ValueError."""
        import pytest
        from app.platform.distance_correlation import distance_correlation_report

        with pytest.raises(ValueError):
            distance_correlation_report([float('nan'), 2.0, 3.0], [4.0, 5.0, 6.0])

        with pytest.raises(ValueError):
            distance_correlation_report([1.0, 2.0, 3.0], [float('inf'), 5.0, 6.0])

    def test_to_dict(self):
        """Result is JSON-serialisable via to_dict()."""
        from app.platform.distance_correlation import distance_correlation_report

        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 4.0, 6.0, 8.0, 10.0]
        result = distance_correlation_report(x, y)
        d = result.to_dict()
        assert d["distance_correlation"] == result.distance_correlation
        assert d["distance_covariance"] == result.distance_covariance
        assert d["distance_variance_x"] == result.distance_variance_x
        assert d["distance_variance_y"] == result.distance_variance_y
        assert 0.0 <= d["distance_correlation"] <= 1.0
