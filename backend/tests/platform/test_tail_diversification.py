"""P388: Tail diversification analysis — unit tests."""

from __future__ import annotations

import math

import pytest

from app.platform.tail_diversification import tail_diversification_report


def _make_panel(n: int = 100) -> dict[str, list[float]]:
    """Create a 2-asset panel with n observations."""
    import random

    rng = random.Random(42)
    return {
        "x": [rng.gauss(0, 1) / 100 for _ in range(n)],
        "y": [rng.gauss(0, 1) / 100 for _ in range(n)],
    }


class TestTailDiversification:
    """Tail diversification unit tests."""

    def test_two_assets_produces_finite_statistics(self) -> None:
        """Two assets with weights produce finite statistics."""
        panel = _make_panel(100)
        weights = {"x": 0.5, "y": 0.5}
        result = tail_diversification_report(panel, weights)
        d = result.to_dict()
        assert math.isfinite(d["portfolio_tail_var"])
        assert math.isfinite(d["benchmark_tail_var"])
        assert math.isfinite(d["tail_diversification_benefit"])
        assert math.isfinite(d["tail_correlation"])
        assert math.isfinite(d["normal_correlation"])
        assert math.isfinite(d["correlation_breakdown"])

    def test_all_statistics_are_finite(self) -> None:
        """All returned statistics are finite floats."""
        panel = _make_panel(100)
        weights = {"x": 0.4, "y": 0.6}
        result = tail_diversification_report(panel, weights)
        for val in [
            result.portfolio_tail_var,
            result.benchmark_tail_var,
            result.tail_diversification_benefit,
            result.tail_correlation,
            result.normal_correlation,
            result.correlation_breakdown,
        ]:
            assert math.isfinite(val)

    def test_equal_weights_benefit_near_zero(self) -> None:
        """Equal weights relative to equal-weight benchmark -> benefit approx 0."""
        panel = _make_panel(100)
        weights = {"x": 0.5, "y": 0.5}
        result = tail_diversification_report(panel, weights)
        assert abs(result.tail_diversification_benefit) < 0.01

    def test_correlation_breakdown_direction(self) -> None:
        """correlation_breakdown = tail_correlation - normal_correlation."""
        panel = _make_panel(100)
        weights = {"x": 0.4, "y": 0.6}
        result = tail_diversification_report(panel, weights)
        assert (
            result.correlation_breakdown
            == result.tail_correlation - result.normal_correlation
        )

    def test_single_asset_raises(self) -> None:
        """At least 2 assets required."""
        panel: dict[str, list[float]] = {
            "a": [0.01, 0.02, 0.03],
        }
        weights = {"a": 1.0}
        with pytest.raises(ValueError):
            tail_diversification_report(panel, weights)

    def test_weights_key_mismatch_raises(self) -> None:
        """Weights keys must match panel keys."""
        panel = _make_panel(100)
        weights = {"x": 0.5, "c": 0.5}
        with pytest.raises(ValueError):
            tail_diversification_report(panel, weights)

    def test_weights_not_sum_to_one_raises(self) -> None:
        """Weights must sum approximately to 1.0."""
        panel = _make_panel(100)
        weights = {"x": 10.0, "y": 20.0}
        with pytest.raises(ValueError):
            tail_diversification_report(panel, weights)

    def test_unequal_length_raises(self) -> None:
        """Unequal return series lengths raise ValueError."""
        panel: dict[str, list[float]] = {
            "a": [0.01, 0.02, 0.03, 0.04, 0.05],
            "b": [0.01, 0.02, 0.03],
        }
        weights = {"a": 0.5, "b": 0.5}
        with pytest.raises(ValueError):
            tail_diversification_report(panel, weights)

    def test_to_dict_all_keys(self) -> None:
        """to_dict contains all expected keys."""
        panel = _make_panel(100)
        weights = {"x": 0.4, "y": 0.6}
        result = tail_diversification_report(panel, weights)
        d = result.to_dict()
        for key in (
            "portfolio_tail_var",
            "benchmark_tail_var",
            "tail_diversification_benefit",
            "tail_correlation",
            "normal_correlation",
            "correlation_breakdown",
        ):
            assert key in d

    def test_custom_threshold(self) -> None:
        """Custom tail threshold works."""
        panel = _make_panel(100)
        weights = {"x": 0.3, "y": 0.7}
        result = tail_diversification_report(panel, weights, threshold=0.1)
        assert math.isfinite(result.portfolio_tail_var)
