"""Tests for P364 capacity scaling."""

from __future__ import annotations

import pytest

from app.platform.capacity_scaling import capacity_scaling_report


class TestCapacityScalingReport:
    def test_scaling_curve_nonempty(self):
        """scaling_curve is non-empty."""
        returns = [0.001, -0.002, 0.003, 0.001, -0.001, 0.002, 0.001, -0.001, 0.002, 0.001]
        result = capacity_scaling_report(returns, adv=1e6, turnover=0.5)
        body = result.to_dict()
        assert len(body["scaling_curve"]) > 0

    def test_net_sharpe_decreases_with_aum(self):
        """net_sharpe monotonically decreases as AUM grows."""
        returns = [0.001] * 100  # all positive returns
        result = capacity_scaling_report(returns, adv=1e6, turnover=0.5)
        body = result.to_dict()
        curve = body["scaling_curve"]
        prev_sharpe = float("inf")
        for point in curve:
            assert point["net_sharpe"] <= prev_sharpe
            prev_sharpe = point["net_sharpe"]

    def test_capacity_limit_is_finite(self):
        """capacity_limit is a finite number."""
        returns = [0.001] * 100
        result = capacity_scaling_report(returns, adv=1e6, turnover=0.5)
        body = result.to_dict()
        assert isinstance(body["capacity_limit"], float)
        import math
        assert math.isfinite(body["capacity_limit"])

    def test_impact_cost_increases_with_aum(self):
        """impact_cost monotonically increases with AUM."""
        returns = [0.001] * 100
        result = capacity_scaling_report(returns, adv=1e6, turnover=0.5)
        body = result.to_dict()
        curve = body["scaling_curve"]
        prev_impact = -1.0
        for point in curve:
            assert point["impact_cost"] >= prev_impact
            prev_impact = point["impact_cost"]

    def test_gross_sharpe_constant(self):
        """gross_sharpe is the same across all AUM levels."""
        returns = [0.001] * 100
        result = capacity_scaling_report(returns, adv=1e6, turnover=0.5)
        body = result.to_dict()
        curve = body["scaling_curve"]
        gross_sharpes = {p["gross_sharpe"] for p in curve}
        assert len(gross_sharpes) == 1

    def test_custom_aum_multipliers(self):
        """Custom aum_multipliers produces expected curve length."""
        returns = [0.001] * 100
        result = capacity_scaling_report(
            returns, adv=1e6, turnover=0.5, aum_multipliers=[0.01, 0.1, 1.0]
        )
        body = result.to_dict()
        assert len(body["scaling_curve"]) == 3

    def test_rejects_empty_returns(self):
        with pytest.raises(ValueError):
            capacity_scaling_report([], adv=1e6, turnover=0.5)

    def test_sharpe_decay_rate_present(self):
        """sharpe_decay_rate is a finite float."""
        returns = [0.001] * 100
        result = capacity_scaling_report(returns, adv=1e6, turnover=0.5)
        body = result.to_dict()
        assert isinstance(body["sharpe_decay_rate"], float)
        import math
        assert math.isfinite(body["sharpe_decay_rate"])
