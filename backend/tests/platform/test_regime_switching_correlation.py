"""Tests for P366 regime-switching correlation module."""
from __future__ import annotations

import math
import pytest
from app.platform.regime_switching_correlation import (
    RegimeSwitchingCorrelationResult,
    regime_switching_correlation_report,
)


class TestRegimeSwitchingCorrelation:
    def test_two_asset_panel_regime_non_empty(self) -> None:
        """Construct 2-asset panel, assert regime_path non-empty and diversification_premium numeric."""
        # Two assets: one trending up, one mean-reverting (low correlation regime)
        a = [0.001 * i for i in range(50)]
        b = [0.001 * (1.0 if i % 3 == 0 else -1.0) for i in range(50)]
        panel = {"asset_a": a, "asset_b": b}
        result = regime_switching_correlation_report(panel, window=20)
        assert isinstance(result, RegimeSwitchingCorrelationResult)
        assert len(result.regime_path) > 0
        assert len(result.avg_correlation_series) > 0
        assert isinstance(result.diversification_premium, float)
        assert math.isfinite(result.diversification_premium)

    def test_high_correlation_panel_mostly_high_regime(self) -> None:
        """Two highly correlated assets should be in high regime most of the time."""
        a = [0.001 * i for i in range(100)]
        b = [0.001 * i + 0.0001 for i in range(100)]
        panel = {"a": a, "b": b}
        result = regime_switching_correlation_report(panel, window=20)
        high_count = sum(1 for r in result.regime_path if r == "high")
        low_count = sum(1 for r in result.regime_path if r == "low")
        assert high_count >= low_count

    def test_regime_stats_present(self) -> None:
        """high_regime_stats and low_regime_stats should have expected fields."""
        a = [0.001 * i for i in range(100)]
        b = [0.002 * (1.0 if i % 2 == 0 else -1.0) for i in range(100)]
        panel = {"a": a, "b": b}
        result = regime_switching_correlation_report(panel, window=20)
        for key in ("mean_corr", "frequency", "avg_duration"):
            assert key in result.high_regime_stats
            assert key in result.low_regime_stats
        assert isinstance(result.high_regime_stats["mean_corr"], float)
        assert isinstance(result.low_regime_stats["mean_corr"], float)

    def test_to_dict(self) -> None:
        """to_dict returns all expected keys."""
        a = [0.001 * i for i in range(60)]
        b = [0.001 * (1.0 if i % 3 == 0 else -1.0) for i in range(60)]
        panel = {"a": a, "b": b}
        result = regime_switching_correlation_report(panel, window=20)
        d = result.to_dict()
        assert "regime_path" in d
        assert "avg_correlation_series" in d
        assert "high_regime_stats" in d
        assert "low_regime_stats" in d
        assert "diversification_premium" in d
        assert "n_assets" in d

    def test_empty_panel_raises(self) -> None:
        with pytest.raises(ValueError):
            regime_switching_correlation_report({}, window=20)

    def test_single_asset_raises(self) -> None:
        with pytest.raises(ValueError):
            regime_switching_correlation_report({"a": [0.001] * 50}, window=20)

    def test_panel_too_many_assets_raises(self) -> None:
        panel = {f"asset_{i}": [0.001] * 50 for i in range(51)}
        with pytest.raises(ValueError):
            regime_switching_correlation_report(panel, window=20)

    def test_unequal_length_series_raises(self) -> None:
        with pytest.raises(ValueError):
            regime_switching_correlation_report(
                {"a": [0.001] * 50, "b": [0.001] * 30}, window=20
            )

    def test_non_finite_values_raise(self) -> None:
        with pytest.raises(ValueError):
            regime_switching_correlation_report(
                {"a": [float("nan"), 0.001], "b": [0.001, 0.002]}, window=20
            )
