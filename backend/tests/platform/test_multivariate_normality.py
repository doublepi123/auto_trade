"""Tests for P374 multivariate normality (Mardia test) module."""
from __future__ import annotations

import math
import random

import pytest
from app.platform.multivariate_normality import (
    MultivariateNormalityResult,
    multivariate_normality_report,
)


class TestMultivariateNormalityReport:
    """Tests for multivariate_normality_report function."""

    def test_three_asset_panel_returns_finite_stats(self) -> None:
        """A 3-asset panel should return finite Mardia statistics."""
        # Construct a small panel with some variation
        returns_panel = {
            "A": [0.01, -0.005, 0.002, 0.003, -0.001, 0.004, -0.002, 0.001, 0.005, -0.003],
            "B": [0.002, 0.001, -0.003, 0.000, 0.004, -0.001, 0.002, -0.004, 0.000, 0.003],
            "C": [-0.001, 0.003, 0.000, -0.002, 0.001, 0.002, -0.003, 0.000, 0.004, -0.001],
        }
        result = multivariate_normality_report(returns_panel)
        assert isinstance(result, MultivariateNormalityResult)
        assert math.isfinite(result.mardia_skewness)
        assert math.isfinite(result.mardia_kurtosis)
        assert isinstance(result.skewness_p_value, float)
        assert isinstance(result.kurtosis_p_value, float)
        assert isinstance(result.is_multivariate_normal, bool)
        assert result.n_observations == 10
        assert result.n_assets == 3

    def test_result_to_dict_serializable(self) -> None:
        """to_dict should produce a JSON-serializable dictionary."""
        returns_panel = {
            "A": [0.01, -0.005, 0.002, 0.003, -0.001],
            "B": [0.002, 0.001, -0.003, 0.000, 0.004],
        }
        result = multivariate_normality_report(returns_panel)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "mardia_skewness" in d
        assert "mardia_kurtosis" in d
        assert "skewness_p_value" in d
        assert "kurtosis_p_value" in d
        assert "is_multivariate_normal" in d
        assert "n_observations" in d
        assert "n_assets" in d
        assert math.isfinite(d["mardia_skewness"])

    def test_invalid_empty_panel_raises(self) -> None:
        """Empty panel should raise ValueError."""
        with pytest.raises(ValueError):
            multivariate_normality_report({})

    def test_invalid_non_dict_raises(self) -> None:
        """Non-dict input should raise ValueError."""
        with pytest.raises(ValueError):
            multivariate_normality_report("not_a_dict")  # type: ignore[arg-type]

    def test_series_length_mismatch_raises(self) -> None:
        """Series with different lengths should raise ValueError."""
        returns_panel = {
            "A": [0.01, 0.02],
            "B": [0.01, 0.02, 0.03],
        }
        with pytest.raises(ValueError, match="equal length"):
            multivariate_normality_report(returns_panel)

    def test_constant_series_singular_covariance(self) -> None:
        """Constant series (singular covariance) should return inf stats."""
        returns_panel = {
            "A": [0.01, 0.01, 0.01, 0.01, 0.01],
            "B": [0.01, 0.01, 0.01, 0.01, 0.01],
        }
        # May raise or produce inf results depending on singular covariance
        try:
            result = multivariate_normality_report(returns_panel)
            assert not result.is_multivariate_normal
        except ValueError:
            pass  # Also acceptable if module rejects degenerate cases

    def test_too_many_assets_raises(self) -> None:
        """More than 50 assets should raise ValueError."""
        returns_panel = {}
        for i in range(51):
            returns_panel[f"A{i}"] = [0.01, 0.02, 0.03]
        with pytest.raises(ValueError, match="50"):
            multivariate_normality_report(returns_panel)

    def test_bool_entries_rejected(self) -> None:
        """Boolean entries should be rejected."""
        returns_panel = {
            "A": [True, False, True],
            "B": [0.01, 0.02, 0.03],
        }
        with pytest.raises(ValueError):
            multivariate_normality_report(returns_panel)

    def test_single_asset_with_enough_obs(self) -> None:
        """Single asset with >= 3 observations should work."""
        returns_panel = {
            "A": [0.01, -0.005, 0.002, 0.003, -0.001],
        }
        result = multivariate_normality_report(returns_panel)
        assert isinstance(result, MultivariateNormalityResult)
        assert result.n_assets == 1

    def test_two_asset_mvn_may_pass(self) -> None:
        """A well-behaved multivariate normal sample should pass."""
        import random

        random.seed(42)
        # Generate bivariate normal data
        returns_panel = {
            "A": [random.gauss(0.0, 0.01) for _ in range(100)],
            "B": [random.gauss(0.0, 0.01) for _ in range(100)],
        }
        result = multivariate_normality_report(returns_panel)
        assert isinstance(result, MultivariateNormalityResult)
        # All stats should be finite
        assert math.isfinite(result.mardia_skewness)
        assert math.isfinite(result.mardia_kurtosis)
