"""Tests for P368 liquidity-adjusted IR module."""
from __future__ import annotations

import math
import pytest
from app.platform.liquidity_adjusted_ir import (
    LiquidityAdjustedIRResult,
    liquidity_adjusted_ir_report,
)


class TestLiquidityAdjustedIR:
    def test_liquidity_drag_non_negative(self) -> None:
        """With positive returns + volumes, liquidity_drag >= 0."""
        returns = [0.001, 0.002, 0.0015, 0.003, 0.002]
        volumes = [1000.0, 1200.0, 1100.0, 1500.0, 1300.0]
        result = liquidity_adjusted_ir_report(
            returns, volumes, spread_bps=5.0, turnover=0.1, periods_per_year=252
        )
        assert isinstance(result, LiquidityAdjustedIRResult)
        assert result.liquidity_drag >= 0.0
        assert result.traditional_ir >= result.liquidity_adjusted_ir

    def test_cost_decomposition_fields(self) -> None:
        """cost_decomposition should have spread and impact keys."""
        returns = [0.001, 0.002, 0.0015]
        volumes = [1000.0, 1200.0, 1100.0]
        result = liquidity_adjusted_ir_report(returns, volumes)
        assert "spread" in result.cost_decomposition
        assert "impact" in result.cost_decomposition
        assert result.cost_decomposition["spread"] >= 0.0
        assert result.cost_decomposition["impact"] >= 0.0

    def test_zero_returns_produces_finite_ir(self) -> None:
        """Zero returns should produce 0 IR and finite results."""
        returns = [0.0, 0.0, 0.0, 0.0, 0.0]
        volumes = [1000.0] * 5
        result = liquidity_adjusted_ir_report(returns, volumes)
        assert result.traditional_ir == 0.0
        assert math.isfinite(result.liquidity_adjusted_ir)

    def test_negative_returns_liquidity_drag(self) -> None:
        """Negative returns should still produce valid results."""
        returns = [-0.001, -0.002, -0.0015]
        volumes = [1000.0, 1200.0, 1100.0]
        result = liquidity_adjusted_ir_report(returns, volumes)
        assert math.isfinite(result.traditional_ir)
        assert math.isfinite(result.liquidity_adjusted_ir)
        assert math.isfinite(result.liquidity_drag)

    def test_to_dict(self) -> None:
        returns = [0.001, 0.002, 0.0015]
        volumes = [1000.0, 1200.0, 1100.0]
        result = liquidity_adjusted_ir_report(returns, volumes)
        d = result.to_dict()
        assert "traditional_ir" in d
        assert "liquidity_adjusted_ir" in d
        assert "liquidity_drag" in d
        assert "cost_decomposition" in d

    def test_empty_returns_raises(self) -> None:
        with pytest.raises(ValueError):
            liquidity_adjusted_ir_report([], [1000.0])

    def test_mismatched_length_raises(self) -> None:
        with pytest.raises(ValueError):
            liquidity_adjusted_ir_report([0.001, 0.002], [1000.0])

    def test_zero_spread_ir_unchanged(self) -> None:
        """With zero spread and very large volumes (negligible impact), drag ~ 0."""
        returns = [0.001, 0.002, 0.0015, 0.003, 0.002]
        volumes = [1e10] * 5
        result = liquidity_adjusted_ir_report(
            returns, volumes, spread_bps=0.0, turnover=0.1, periods_per_year=252
        )
        # With zero spread and huge volumes, liquidity_drag should be very small
        # relative to the traditional IR
        assert result.liquidity_drag < abs(result.traditional_ir) * 0.001

    def test_non_finite_returns_raise(self) -> None:
        with pytest.raises(ValueError):
            liquidity_adjusted_ir_report([float("nan"), 0.001], [1000.0, 1000.0])
