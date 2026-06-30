"""Tests for P383 quadratic factor model.

Validates OLS regression with linear + squared + interaction terms.
Asserts r_squared > 0.5 for nonlinear returns = factor² relationship.
"""

from __future__ import annotations

import math

import pytest

from app.platform.quadratic_factor_model import (
    QuadraticFactorModelResult,
    quadratic_factor_model_report,
)


class TestQuadraticFactorModelReport:
    """Unit tests for quadratic_factor_model_report."""

    def test_quadratic_returns_r_squared_high(self):
        """returns = f^2 → r_squared > 0.5."""
        n = 100
        f = [i / n * 2.0 - 1.0 for i in range(n)]  # [-1, 1]
        returns = [v * v for v in f]  # pure quadratic
        factors = {"factor1": f}

        result = quadratic_factor_model_report(returns, factors)

        assert isinstance(result, QuadraticFactorModelResult)
        assert result.r_squared > 0.5
        assert "const" in result.coefficients
        assert "factor1" in result.coefficients
        assert "factor1^2" in result.coefficients
        # Quadratic coefficient should be close to 1 for f^2 returns
        assert abs(result.coefficients["factor1^2"] - 1.0) < 0.2

    def test_linear_returns(self):
        """returns = 2*f → linear model works, quadratic adds little."""
        n = 100
        f = [i / n * 2.0 - 1.0 for i in range(n)]
        returns = [2.0 * v + 0.5 for v in f]  # linear
        factors = {"factor1": f}

        result = quadratic_factor_model_report(returns, factors)

        assert result.r_squared > 0.8
        assert result.linear_vs_quadratic_comparison["r_squared_improvement"] < 0.1
        assert result.nonlinear_significance < 5.0  # F-stat should be low

    def test_multiple_factors(self):
        """Two factors with interaction."""
        n = 200
        import random
        rng = random.Random(42)
        f1 = [rng.gauss(0, 0.02) for _ in range(n)]
        f2 = [rng.gauss(0, 0.01) for _ in range(n)]
        # returns = f1 + f1*f2 + f2^2
        returns = [f1[i] + f1[i] * f2[i] + f2[i] * f2[i] for i in range(n)]
        factors = {"f1": f1, "f2": f2}

        result = quadratic_factor_model_report(returns, factors)

        assert isinstance(result, QuadraticFactorModelResult)
        assert "const" in result.coefficients
        assert "f1" in result.coefficients
        assert "f2" in result.coefficients
        assert "f1^2" in result.coefficients
        assert "f2^2" in result.coefficients
        assert "f1*f2" in result.coefficients
        assert result.r_squared > 0.0

    def test_mismatched_lengths_raises(self):
        """Factor and returns different lengths raises."""
        with pytest.raises(ValueError, match="must have length"):
            quadratic_factor_model_report(
                [0.01, 0.02, 0.03],
                {"f": [0.1, 0.2]},
            )

    def test_empty_returns_raises(self):
        """Empty returns raises."""
        with pytest.raises(ValueError, match="non-empty"):
            quadratic_factor_model_report([], {"f": [0.1]})

    def test_empty_factors_raises(self):
        """Empty factors raises."""
        # Need at least 3 returns to pass the min_len check before factor validation
        with pytest.raises(ValueError, match="non-empty"):
            quadratic_factor_model_report([0.01, 0.02, 0.03], {})

    def test_non_list_returns_raises(self):
        """Non-list returns raises."""
        with pytest.raises(ValueError):
            quadratic_factor_model_report("abc", {"f": [0.1]})  # type: ignore[arg-type]

    def test_non_numeric_raises(self):
        """Non-numeric values raise."""
        with pytest.raises(ValueError):
            quadratic_factor_model_report([0.01, "bad"], {"f": [0.1, 0.2]})  # type: ignore[list-item]

    def test_singular_design_raises(self):
        """Perfectly collinear factors should raise or handle gracefully."""
        # Constant factor → constant factor^2 = constant → near-singular
        n = 10
        f = [1.0] * n
        returns = [0.01] * n
        # This creates a linearly dependent design matrix (const, f, f^2 all collinear)
        with pytest.raises(ValueError):
            quadratic_factor_model_report(returns, {"f": f})

    def test_to_dict_roundtrip(self):
        """to_dict includes all expected keys."""
        f = [i / 100.0 for i in range(100)]
        returns = [v * v for v in f]
        result = quadratic_factor_model_report(returns, {"f": f})
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "coefficients" in d
        assert "r_squared" in d
        assert "nonlinear_significance" in d
        assert "linear_vs_quadratic_comparison" in d
