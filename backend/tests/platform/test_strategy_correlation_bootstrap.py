"""P387: Strategy correlation bootstrap — unit tests."""

from __future__ import annotations

import math

import pytest

from app.platform.strategy_correlation_bootstrap import (
    strategy_correlation_bootstrap_report,
)


class TestStrategyCorrelationBootstrap:
    """Strategy correlation bootstrap unit tests."""

    def test_two_strategies_produce_correlation_matrix(self) -> None:
        """Two strategies produce a correlation matrix with one pair entry."""
        returns: dict[str, list[float]] = {
            "s1": [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.01, 0.02, -0.01, 0.01],
            "s2": [0.015, 0.025, -0.005, 0.035, 0.005, -0.015, 0.015, 0.025, -0.005, 0.015],
        }
        result = strategy_correlation_bootstrap_report(returns, n_bootstrap=100, seed=42)
        d = result.to_dict()
        assert len(d["correlation_matrix"]) == 1
        assert "s1|s2" in d["correlation_matrix"]
        entry = d["correlation_matrix"]["s1|s2"]
        for key in ("mean", "ci_lower", "ci_upper"):
            assert key in entry
            assert math.isfinite(entry[key])

    def test_correlation_matrix_non_empty(self) -> None:
        """correlation_matrix is non-empty for any valid input."""
        returns: dict[str, list[float]] = {
            "a": [0.01, -0.02, 0.03, -0.01, 0.02],
            "b": [-0.01, 0.02, -0.03, 0.01, -0.02],
        }
        result = strategy_correlation_bootstrap_report(returns, n_bootstrap=50, seed=1)
        assert len(result.correlation_matrix) > 0

    def test_each_pair_has_ci(self) -> None:
        """Each strategy pair has mean, ci_lower, ci_upper."""
        returns: dict[str, list[float]] = {
            "x": [0.01, 0.02, -0.01, 0.03, -0.02, 0.0, 0.01, 0.02, -0.01, 0.01],
            "y": [0.02, 0.03, 0.0, 0.04, -0.01, 0.01, 0.02, 0.03, 0.0, 0.02],
            "z": [-0.01, -0.02, 0.01, -0.03, 0.02, 0.0, -0.01, -0.02, 0.01, -0.01],
        }
        result = strategy_correlation_bootstrap_report(returns, n_bootstrap=100, seed=42)
        d = result.to_dict()
        # 3 strategies → 3 pairs
        assert len(d["correlation_matrix"]) == 3
        for pair_key in ("x|y", "x|z", "y|z"):
            assert pair_key in d["correlation_matrix"]
            entry = d["correlation_matrix"][pair_key]
            assert "mean" in entry
            assert entry["ci_lower"] <= entry["mean"] <= entry["ci_upper"]

    def test_single_strategy_raises(self) -> None:
        """At least 2 strategies required."""
        returns: dict[str, list[float]] = {"s": [0.01, 0.02]}
        with pytest.raises(ValueError):
            strategy_correlation_bootstrap_report(returns)

    def test_unequal_length_raises(self) -> None:
        """Unequal return series lengths raise ValueError."""
        returns: dict[str, list[float]] = {
            "a": [0.01, 0.02, 0.03],
            "b": [0.01, 0.02],
        }
        with pytest.raises(ValueError):
            strategy_correlation_bootstrap_report(returns)

    def test_significant_pairs_list(self) -> None:
        """significant_pairs is a list of pair names (possibly empty)."""
        returns: dict[str, list[float]] = {
            "s1": [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.01, 0.02, -0.01, 0.01],
            "s2": [0.015, 0.025, -0.005, 0.035, 0.005, -0.015, 0.015, 0.025, -0.005, 0.015],
        }
        result = strategy_correlation_bootstrap_report(returns, n_bootstrap=100, seed=42)
        assert isinstance(result.significant_pairs, list)

    def test_diversification_significant_is_bool(self) -> None:
        """diversification_significant is a boolean."""
        returns: dict[str, list[float]] = {
            "s1": [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.01, 0.02, -0.01, 0.01],
            "s2": [0.02, 0.03, -0.02, 0.05, 0.01, -0.03, 0.02, 0.03, -0.02, 0.02],
        }
        result = strategy_correlation_bootstrap_report(returns, n_bootstrap=50, seed=1)
        assert isinstance(result.diversification_significant, bool)

    def test_block_size_one_edge(self) -> None:
        """Block size 1 should still work (standard bootstrap)."""
        returns: dict[str, list[float]] = {
            "a": [0.01, -0.02, 0.03, -0.01, 0.02, -0.01, 0.02, 0.0, -0.01, 0.01],
            "b": [-0.01, 0.02, -0.03, 0.01, -0.02, 0.01, -0.02, 0.0, 0.01, -0.01],
        }
        result = strategy_correlation_bootstrap_report(
            returns, n_bootstrap=50, seed=42, block_size=1
        )
        assert len(result.correlation_matrix) == 1

    def test_to_dict_all_keys(self) -> None:
        """to_dict contains all expected keys."""
        returns: dict[str, list[float]] = {
            "s1": [0.01, 0.02, -0.01, 0.03, 0.0],
            "s2": [0.02, 0.03, 0.0, 0.04, 0.01],
        }
        result = strategy_correlation_bootstrap_report(returns, n_bootstrap=30, seed=42)
        d = result.to_dict()
        for key in ("correlation_matrix", "significant_pairs", "diversification_significant"):
            assert key in d
