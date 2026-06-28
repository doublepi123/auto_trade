"""P335: multi-strategy risk report tests."""

import math

import pytest

from app.platform.multi_strategy_risk import (
    MultiStrategyRiskResult,
    multi_strategy_risk_report,
)


class TestMultiStrategyRisk:
    def test_basic_two_strategy(self) -> None:
        returns = {
            "strat_a": [0.01, -0.005, 0.02, -0.01, 0.005],
            "strat_b": [0.005, 0.01, -0.01, 0.02, -0.005],
        }
        weights = {"strat_a": 0.6, "strat_b": 0.4}
        result = multi_strategy_risk_report(returns, weights)

        assert isinstance(result, MultiStrategyRiskResult)
        assert result.portfolio_vol > 0
        assert "strat_a" in result.risk_contributions
        assert "strat_b" in result.risk_contributions
        # Risk contributions should sum to portfolio_vol (within tolerance)
        contrib_sum = sum(result.risk_contributions.values())
        assert math.isclose(contrib_sum, result.portfolio_vol, rel_tol=1e-9)
        assert result.diversification_ratio > 0
        assert result.concentration_hhi > 0
        assert isinstance(result.covariance_matrix, list)
        assert len(result.covariance_matrix) == 2

    def test_three_strategy(self) -> None:
        returns = {
            "a": [0.02, -0.01, 0.01, 0.03, -0.02],
            "b": [-0.01, 0.02, 0.01, -0.01, 0.03],
            "c": [0.01, 0.01, -0.02, 0.02, 0.01],
        }
        weights = {"a": 0.4, "b": 0.35, "c": 0.25}
        result = multi_strategy_risk_report(returns, weights)

        assert result.portfolio_vol > 0
        contrib_sum = sum(result.risk_contributions.values())
        assert math.isclose(contrib_sum, result.portfolio_vol, rel_tol=1e-9)
        assert len(result.covariance_matrix) == 3

    def test_identical_returns(self) -> None:
        """Two strategies with identical returns: diversification_ratio should be 1."""
        returns = {
            "x": [0.01, -0.005, 0.02],
            "y": [0.01, -0.005, 0.02],
        }
        weights = {"x": 0.5, "y": 0.5}
        result = multi_strategy_risk_report(returns, weights)

        # With identical returns, diversification should be minimal
        assert math.isclose(result.diversification_ratio, 1.0, rel_tol=1e-9)

    def test_invalid_empty_returns(self) -> None:
        with pytest.raises(ValueError):
            multi_strategy_risk_report({}, {"a": 1.0})

    def test_invalid_missing_weight(self) -> None:
        returns = {"a": [0.01, -0.01], "b": [0.02, -0.02]}
        with pytest.raises(ValueError):
            multi_strategy_risk_report(returns, {"a": 1.0})

    def test_invalid_unequal_length(self) -> None:
        returns = {"a": [0.01, -0.01], "b": [0.02]}
        weights = {"a": 0.5, "b": 0.5}
        with pytest.raises(ValueError):
            multi_strategy_risk_report(returns, weights)

    def test_invalid_negative_weight(self) -> None:
        returns = {"a": [0.01, -0.01], "b": [0.02, -0.02]}
        with pytest.raises(ValueError):
            multi_strategy_risk_report(returns, {"a": -0.5, "b": 1.5})

    def test_concentration_hhi_fully_concentrated(self) -> None:
        """When one strategy has weight 1 and others 0, HHI should be 1."""
        returns = {
            "a": [0.01, -0.005, 0.02],
            "b": [0.005, 0.01, -0.01],
        }
        weights = {"a": 1.0, "b": 0.0}
        result = multi_strategy_risk_report(returns, weights)
        assert math.isclose(result.concentration_hhi, 1.0, rel_tol=1e-9)

    def test_to_dict(self) -> None:
        returns = {"a": [0.01, -0.005, 0.02]}
        weights = {"a": 1.0}
        result = multi_strategy_risk_report(returns, weights)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "portfolio_vol" in d
        assert "risk_contributions" in d
        assert "diversification_ratio" in d
        assert "concentration_hhi" in d
        assert "covariance_matrix" in d

    def test_invalid_non_finite(self) -> None:
        returns = {"a": [float("nan"), 0.01]}
        weights = {"a": 1.0}
        with pytest.raises(ValueError):
            multi_strategy_risk_report(returns, weights)

    def test_invalid_empty_return_list(self) -> None:
        returns = {"a": []}
        weights = {"a": 1.0}
        with pytest.raises(ValueError):
            multi_strategy_risk_report(returns, weights)


def test_multi_strategy_risk_rejects_zero_periods_per_year():
    from app.platform.multi_strategy_risk import multi_strategy_risk_report
    with pytest.raises(ValueError):
        multi_strategy_risk_report({"a": [0.01, -0.01, 0.02]}, {"a": 1.0}, periods_per_year=0)
