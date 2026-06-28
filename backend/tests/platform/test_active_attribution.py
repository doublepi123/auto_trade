"""P358: Active Attribution — unit tests.

Decompose active return (returns - benchmark) into factor contribution and
residual alpha. Pure-Python computation.
"""

from __future__ import annotations

import pytest

from app.platform.active_attribution import (
    ActiveAttributionResult,
    active_attribution_report,
)


class TestActiveAttribution:
    """Active attribution unit tests."""

    def test_active_return_stats_basic(self) -> None:
        """active_return_stats includes mean and t_stat."""
        returns = [0.02, 0.01, -0.01, 0.03, 0.00]
        benchmark = [0.01, 0.01, 0.00, 0.01, 0.01]
        result = active_attribution_report(returns, benchmark)
        stats = result.active_return_stats
        assert "mean" in stats
        assert "t_stat" in stats
        assert "std" in stats
        assert "information_ratio" in stats

    def test_active_return_mean_correct(self) -> None:
        """Active return mean = mean(returns - benchmark)."""
        returns = [0.02, 0.01, -0.01, 0.03, 0.00]
        benchmark = [0.01, 0.01, 0.00, 0.01, 0.01]
        result = active_attribution_report(returns, benchmark)
        active = [r - b for r, b in zip(returns, benchmark)]
        expected_mean = sum(active) / len(active)
        assert abs(result.active_return_stats["mean"] - expected_mean) < 1e-9

    def test_factor_decomposition(self) -> None:
        """When factor_exposures + factor_returns are provided, factor_contribution is present."""
        returns = [0.02, 0.01, -0.01, 0.03, 0.00]
        benchmark = [0.01, 0.01, 0.00, 0.01, 0.01]
        factor_exposures: dict[str, list[float]] = {
            "market": [1.0, 1.0, 1.0, 1.0, 1.0],
        }
        factor_returns: dict[str, list[float]] = {
            "market": [0.01, 0.00, -0.01, 0.02, 0.00],
        }
        result = active_attribution_report(
            returns, benchmark,
            factor_exposures=factor_exposures,
            factor_returns=factor_returns,
        )
        assert result.factor_contribution is not None
        assert "market" in result.factor_contribution
        assert result.residual_alpha is not None

    def test_residual_alpha_is_active_minus_factor(self) -> None:
        """residual_alpha = active_return - sum(exposure_i * factor_return_i)."""
        returns = [0.02, 0.01, -0.01]
        benchmark = [0.01, 0.00, 0.00]
        factor_exposures: dict[str, list[float]] = {
            "f1": [0.5, 0.5, 0.5],
        }
        factor_returns: dict[str, list[float]] = {
            "f1": [0.02, 0.01, -0.01],
        }
        result = active_attribution_report(
            returns, benchmark,
            factor_exposures=factor_exposures,
            factor_returns=factor_returns,
        )
        active = [r - b for r, b in zip(returns, benchmark)]
        factor_contrib = [0.5 * fr for fr in factor_returns["f1"]]
        expected_residual = [a - fc for a, fc in zip(active, factor_contrib)]
        assert result.residual_alpha is not None
        for actual, expected in zip(result.residual_alpha, expected_residual):
            assert abs(actual - expected) < 1e-9

    def test_no_factor_gives_none_contribution(self) -> None:
        """Without factors, factor_contribution and residual_alpha are None."""
        returns = [0.02, -0.01, 0.03]
        benchmark = [0.01, 0.00, 0.01]
        result = active_attribution_report(returns, benchmark)
        assert result.factor_contribution is None
        assert result.residual_alpha is None

    def test_summary_present(self) -> None:
        """summary string is present in result."""
        returns = [0.02, -0.01, 0.03]
        benchmark = [0.01, 0.00, 0.01]
        result = active_attribution_report(returns, benchmark)
        assert isinstance(result.summary, str)
        assert len(result.summary) > 0

    def test_mismatched_lengths_raises_value_error(self) -> None:
        """Mismatched lengths raise ValueError."""
        returns = [0.01, 0.02]
        benchmark = [0.01]
        with pytest.raises(ValueError):
            active_attribution_report(returns, benchmark)

    def test_mismatched_factor_lengths_raises_value_error(self) -> None:
        """Factor series length mismatch raises ValueError."""
        returns = [0.01, 0.02, 0.03]
        benchmark = [0.00, 0.01, 0.01]
        factor_exposures: dict[str, list[float]] = {"f": [0.5, 0.5]}  # too short
        factor_returns: dict[str, list[float]] = {"f": [0.01, 0.01, 0.01]}
        with pytest.raises(ValueError):
            active_attribution_report(
                returns, benchmark,
                factor_exposures=factor_exposures,
                factor_returns=factor_returns,
            )

    def test_non_numeric_raises_type_error(self) -> None:
        """Non-numeric returns raise TypeError."""
        with pytest.raises(TypeError):
            active_attribution_report(["x", "y"], [0.01, 0.01])  # type: ignore[arg-type]

    def test_to_dict_roundtrip(self) -> None:
        """to_dict returns expected keys."""
        returns = [0.02, -0.01, 0.03]
        benchmark = [0.01, 0.00, 0.01]
        factor_exposures: dict[str, list[float]] = {"f": [1.0, 1.0, 1.0]}
        factor_returns: dict[str, list[float]] = {"f": [0.01, -0.01, 0.02]}
        result = active_attribution_report(
            returns, benchmark,
            factor_exposures=factor_exposures,
            factor_returns=factor_returns,
        )
        d = result.to_dict()
        for key in (
            "active_return_stats",
            "factor_contribution",
            "residual_alpha",
            "summary",
        ):
            assert key in d
