"""P356: Implied Correlation — unit tests.

Solve for the average implied correlation from index IV and constituent stock IVs
using the portfolio-variance identity under equal-weight assumption.
"""

from __future__ import annotations

import pytest

from app.platform.implied_correlation import (
    ImpliedCorrelationResult,
    implied_correlation_report,
)


class TestImpliedCorrelation:
    """Implied correlation unit tests."""

    def test_basic_implied_correlation_in_bounds(self) -> None:
        """With index_iv=0.2 and three stock_ivs, implied_correlation is in [0, 1]."""
        index_iv = 0.20
        stock_ivs: dict[str, float] = {
            "A": 0.22,
            "B": 0.18,
            "C": 0.25,
        }
        result = implied_correlation_report(index_iv, stock_ivs)
        assert 0.0 <= result.implied_correlation <= 1.0

    def test_perfect_correlation_bound(self) -> None:
        """When index_iv equals the equal-weighted sum of stock IVs, rho approaches 1."""
        # If all stock IVs equal the index IV, then rho should be high.
        index_iv = 0.20
        stock_ivs: dict[str, float] = {
            "A": 0.20,
            "B": 0.20,
            "C": 0.20,
        }
        result = implied_correlation_report(index_iv, stock_ivs)
        # With equal IVs, implied_correlation should be near 1 (perfect correlation).
        assert result.implied_correlation > 0.9

    def test_zero_correlation_when_index_var_is_just_idiosyncratic(self) -> None:
        """When index variance equals weighted sum of individual variances, rho ≈ 0."""
        # index_var = sum(w_i^2 * iv_i^2)  → this is the idiosyncratic floor.
        # If index IV is just the equal-weighted IVs' idiosyncratic contribution,
        # rho should be near 0.
        stock_ivs: dict[str, float] = {
            "A": 0.20,
            "B": 0.20,
        }
        # Under equal weight: idiosyncratic var = (1/n^2) * sum(iv_i^2)
        n = 2
        idio_var = sum(iv**2 for iv in stock_ivs.values()) / (n * n)
        index_iv = idio_var**0.5  # This makes the index IV match the idio floor.
        result = implied_correlation_report(index_iv, stock_ivs)
        # Should be near 0 (no systematic component).
        assert result.implied_correlation < 0.01

    def test_variance_decomposition_present(self) -> None:
        """Result includes variance decomposition fields."""
        index_iv = 0.20
        stock_ivs: dict[str, float] = {"A": 0.20, "B": 0.20}
        result = implied_correlation_report(index_iv, stock_ivs)
        assert result.implied_index_variance > 0
        assert result.realized_weighted_variance > 0
        assert "idiosyncratic" in result.variance_decomposition
        assert "systematic" in result.variance_decomposition

    def test_single_stock_raises_value_error(self) -> None:
        """At least 2 stocks are required for correlation."""
        index_iv = 0.20
        stock_ivs: dict[str, float] = {"A": 0.20}
        with pytest.raises(ValueError):
            implied_correlation_report(index_iv, stock_ivs)

    def test_non_numeric_iv_raises_type_error(self) -> None:
        """Non-numeric IV raises TypeError."""
        with pytest.raises(TypeError):
            implied_correlation_report("x", {"A": 0.20})  # type: ignore[arg-type]

    def test_negative_iv_raises_value_error(self) -> None:
        """Negative IV raises ValueError."""
        with pytest.raises(ValueError):
            implied_correlation_report(0.20, {"A": -0.20, "B": 0.20})

    def test_to_dict_roundtrip(self) -> None:
        """to_dict returns all expected keys."""
        index_iv = 0.20
        stock_ivs: dict[str, float] = {"A": 0.22, "B": 0.18, "C": 0.25}
        result = implied_correlation_report(index_iv, stock_ivs)
        d = result.to_dict()
        for key in (
            "implied_correlation",
            "implied_index_variance",
            "realized_weighted_variance",
            "variance_decomposition",
        ):
            assert key in d
