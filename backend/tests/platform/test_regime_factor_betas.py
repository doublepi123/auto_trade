"""P386: Regime factor betas — unit tests."""

from __future__ import annotations

import pytest

from app.platform.regime_factor_betas import regime_factor_betas_report


class TestRegimeFactorBetas:
    """Regime factor betas unit tests."""

    def test_two_regimes_one_factor_has_betas(self) -> None:
        """Each regime returns a beta for the factor."""
        returns = [0.01, 0.02, -0.01, 0.03, 0.01, -0.02]
        factor_returns = {"f1": [0.02, 0.04, 0.01, 0.05, 0.02, -0.01]}
        regimes = ["bull", "bull", "bull", "bear", "bear", "bear"]
        result = regime_factor_betas_report(returns, factor_returns, regimes)
        d = result.to_dict()
        assert "bull" in d["per_regime"]
        assert "bear" in d["per_regime"]
        assert "f1" in d["per_regime"]["bull"]["betas"]
        assert "f1" in d["per_regime"]["bear"]["betas"]
        assert "r_squared" in d["per_regime"]["bull"]
        assert "n" in d["per_regime"]["bull"]

    def test_beta_stability_and_spread_present(self) -> None:
        """beta_stability and regime_beta_spread contain entries for each factor."""
        returns = [0.01, 0.02, -0.01, 0.03, 0.01, -0.02, 0.04, -0.03]
        factor_returns = {"f1": [0.02, 0.04, 0.01, 0.05, 0.02, -0.01, 0.03, -0.02]}
        regimes = ["bull"] * 4 + ["bear"] * 4
        result = regime_factor_betas_report(returns, factor_returns, regimes)
        d = result.to_dict()
        assert "f1" in d["beta_stability"]
        assert "f1" in d["regime_beta_spread"]
        assert isinstance(d["beta_stability"]["f1"], float)
        assert isinstance(d["regime_beta_spread"]["f1"], float)

    def test_mismatched_lengths_raises(self) -> None:
        """Mismatched factor series length raises ValueError."""
        returns = [0.01, 0.02, 0.03, 0.04]
        factor_returns = {"f1": [0.01, 0.02, 0.03]}
        regimes = ["a"] * 4
        with pytest.raises(ValueError):
            regime_factor_betas_report(returns, factor_returns, regimes)

    def test_regime_length_mismatch_raises(self) -> None:
        """Regime list length != returns length raises ValueError."""
        returns = [0.01, 0.02, 0.03]
        factor_returns = {"f1": [0.01, 0.02, 0.03]}
        regimes = ["a", "b"]
        with pytest.raises(ValueError):
            regime_factor_betas_report(returns, factor_returns, regimes)

    def test_single_regime_raises(self) -> None:
        """At least 2 distinct regime labels required."""
        returns = [0.01, 0.02, 0.03, 0.04]
        factor_returns = {"f1": [0.01, 0.02, 0.03, 0.04]}
        regimes = ["a"] * 4
        with pytest.raises(ValueError):
            regime_factor_betas_report(returns, factor_returns, regimes)

    def test_empty_factor_returns_raises(self) -> None:
        """Empty factor_returns dict raises ValueError."""
        returns = [0.01, 0.02, 0.03]
        with pytest.raises(ValueError):
            regime_factor_betas_report(returns, {}, ["a", "b", "c"])

    def test_to_dict_roundtrip(self) -> None:
        """to_dict contains all expected top-level keys."""
        returns = [0.01, 0.02, -0.01, 0.03, 0.01, -0.02]
        factor_returns = {"f1": [0.02, 0.04, 0.01, 0.05, 0.02, -0.01]}
        regimes = ["bull"] * 3 + ["bear"] * 3
        result = regime_factor_betas_report(returns, factor_returns, regimes)
        d = result.to_dict()
        for key in ("per_regime", "beta_stability", "regime_beta_spread"):
            assert key in d

    def test_multiple_factors(self) -> None:
        """Works with multiple factors."""
        returns = [0.01, 0.02, -0.01, 0.03, 0.01, -0.02, 0.04, -0.01]
        factor_returns = {
            "f1": [0.02, 0.04, 0.01, 0.05, 0.02, -0.01, 0.03, -0.02],
            "f2": [0.01, 0.01, -0.02, 0.02, 0.0, -0.01, 0.01, 0.0],
        }
        regimes = ["A"] * 4 + ["B"] * 4
        result = regime_factor_betas_report(returns, factor_returns, regimes)
        d = result.to_dict()
        assert "f1" in d["per_regime"]["A"]["betas"]
        assert "f2" in d["per_regime"]["A"]["betas"]
        assert "f1" in d["per_regime"]["B"]["betas"]
