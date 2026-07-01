"""Tests for P346 option strategy payoff module."""

from __future__ import annotations

import math

import pytest

from app.platform.option_strategy_payoff import (
    OptionStrategyPayoffResult,
    option_strategy_payoff_report,
)


class TestOptionStrategyPayoff:
    def test_single_long_call_breakeven_and_profit(self):
        legs = [{"strike": 100.0, "type": "call", "quantity": 1.0, "premium": 5.0}]
        result = option_strategy_payoff_report(legs)
        assert isinstance(result, OptionStrategyPayoffResult)
        # long call: breakeven = strike + premium = 105
        assert len(result.breakeven_points) == 1
        assert abs(result.breakeven_points[0] - 105.0) < 0.5
        # max_profit is unlimited for a long call — should be None or large
        assert result.max_profit is None or (isinstance(result.max_profit, float) and result.max_profit > 1e6)
        # max_loss = premium paid = -5
        assert result.max_loss is not None
        assert abs(result.max_loss - (-5.0)) < 1e-9
        # total_premium = -5 (paid)
        assert abs(result.total_premium - (-5.0)) < 1e-9
        # payoff_points should have 100 points (default grid)
        assert len(result.payoff_points) == 100

    def test_single_long_put_breakeven_and_profit(self):
        legs = [{"strike": 100.0, "type": "put", "quantity": 1.0, "premium": 3.0}]
        result = option_strategy_payoff_report(legs)
        # long put: breakeven = strike - premium = 97
        assert len(result.breakeven_points) == 1
        assert abs(result.breakeven_points[0] - 97.0) < 0.5
        # max_profit for long put: strike - premium at spot=0 = 97
        assert result.max_profit is not None
        assert abs(result.max_profit - 97.0) < 1.0
        assert result.max_loss is not None
        assert abs(result.max_loss - (-3.0)) < 1e-9
        assert abs(result.total_premium - (-3.0)) < 1e-9

    def test_straddle_two_breakevens(self):
        legs = [
            {"strike": 100.0, "type": "call", "quantity": 1.0, "premium": 5.0},
            {"strike": 100.0, "type": "put", "quantity": 1.0, "premium": 4.0},
        ]
        result = option_strategy_payoff_report(legs)
        # straddle: total premium = -9, breakevens at strike ± total_premium
        assert len(result.breakeven_points) == 2
        assert abs(result.total_premium - (-9.0)) < 1e-9
        # breakevens near 91 and 109
        breakevens_sorted = sorted(result.breakeven_points)
        assert abs(breakevens_sorted[0] - 91.0) < 1.0
        assert abs(breakevens_sorted[1] - 109.0) < 1.0
        # max_profit unlimited
        assert result.max_profit is None or result.max_profit > 1e6
        # max_loss = total premium paid
        assert result.max_loss is not None
        assert abs(result.max_loss - (-9.0)) < 1e-9

    def test_bull_call_spread(self):
        legs = [
            {"strike": 100.0, "type": "call", "quantity": 1.0, "premium": 5.0},
            {"strike": 110.0, "type": "call", "quantity": -1.0, "premium": 2.0},
        ]
        result = option_strategy_payoff_report(legs)
        # net premium = -5 + 2 = -3
        assert abs(result.total_premium - (-3.0)) < 1e-9
        # breakeven = lower strike + net premium = 103
        assert len(result.breakeven_points) == 1
        assert abs(result.breakeven_points[0] - 103.0) < 0.5
        # max_profit = (upper - lower) - net_premium = 10 - 3 = 7
        assert result.max_profit is not None
        assert abs(result.max_profit - 7.0) < 1e-9
        # max_loss = net premium = -3
        assert result.max_loss is not None
        assert abs(result.max_loss - (-3.0)) < 1e-9

    def test_custom_spot_range(self):
        legs = [{"strike": 100.0, "type": "call", "quantity": 1.0, "premium": 5.0}]
        result = option_strategy_payoff_report(legs, spot_range=[80.0, 90.0, 100.0, 110.0, 120.0])
        assert len(result.payoff_points) == 5
        assert result.payoff_points[0]["spot"] == 80.0
        assert result.payoff_points[-1]["spot"] == 120.0

    def test_to_dict(self):
        legs = [{"strike": 100.0, "type": "call", "quantity": 1.0, "premium": 5.0}]
        result = option_strategy_payoff_report(legs)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "payoff_points" in d
        assert "breakeven_points" in d
        assert "max_profit" in d
        assert "max_loss" in d
        assert "total_premium" in d
        assert isinstance(d["payoff_points"], list)
        assert isinstance(d["breakeven_points"], list)

    def test_empty_legs_raises(self):
        with pytest.raises(ValueError):
            option_strategy_payoff_report([])

    def test_invalid_option_type_raises(self):
        with pytest.raises(ValueError):
            option_strategy_payoff_report([
                {"strike": 100.0, "type": "invalid", "quantity": 1.0, "premium": 5.0}
            ])

    def test_missing_field_raises(self):
        with pytest.raises(ValueError):
            option_strategy_payoff_report([
                {"strike": 100.0, "type": "call", "quantity": 1.0}
            ])

    def test_non_finite_strike_raises(self):
        with pytest.raises(ValueError):
            option_strategy_payoff_report([
                {"strike": float("inf"), "type": "call", "quantity": 1.0, "premium": 5.0}
            ])

    def test_single_short_call(self):
        legs = [{"strike": 100.0, "type": "call", "quantity": -1.0, "premium": 5.0}]
        result = option_strategy_payoff_report(legs)
        # short call: breakeven = strike + premium = 105, max_profit = premium = 5
        assert len(result.breakeven_points) == 1
        assert abs(result.breakeven_points[0] - 105.0) < 0.5
        assert result.max_profit is not None
        assert abs(result.max_profit - 5.0) < 1e-9
        assert result.max_loss is None or result.max_loss < -1e6
        assert abs(result.total_premium - 5.0) < 1e-9

    def test_payoff_at_strike(self):
        """Payoff at the strike should equal the intrinsic value: 0 for OTM."""
        legs = [{"strike": 100.0, "type": "call", "quantity": 1.0, "premium": 5.0}]
        result = option_strategy_payoff_report(legs, spot_range=[100.0])
        # At spot = strike, call intrinsic = 0, payoff = -premium = -5
        assert abs(result.payoff_points[0]["payoff"] - (-5.0)) < 1e-9
