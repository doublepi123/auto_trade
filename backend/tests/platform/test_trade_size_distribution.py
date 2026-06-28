"""Tests for P367 trade-size distribution module."""
from __future__ import annotations

import math
import pytest
from app.platform.trade_size_distribution import (
    TradeSizeDistributionResult,
    trade_size_distribution_report,
)


class TestTradeSizeDistribution:
    def test_distribution_stats_has_mean_and_std(self) -> None:
        """Construct a volume series, assert distribution_stats has mean and std."""
        volumes = [100.0, 200.0, 150.0, 300.0, 250.0, 175.0, 225.0, 400.0, 350.0, 275.0]
        result = trade_size_distribution_report(volumes, round_lot=100.0)
        assert isinstance(result, TradeSizeDistributionResult)
        assert "mean" in result.distribution_stats
        assert "std" in result.distribution_stats
        assert result.distribution_stats["mean"] > 0

    def test_pareto_alpha_positive(self) -> None:
        """pareto_alpha should be positive if top values vary."""
        volumes = [100.0 + i * 10 for i in range(50)]
        result = trade_size_distribution_report(volumes, round_lot=100.0)
        if result.pareto_alpha is not None:
            assert result.pareto_alpha > 0

    def test_hurst_exponent_in_range(self) -> None:
        """hurst_exponent should be in [0, 1]."""
        volumes = [100.0 + i * 10 + 50 * (1.0 if i % 2 == 0 else -1.0) for i in range(60)]
        result = trade_size_distribution_report(volumes, round_lot=100.0)
        if result.hurst_exponent is not None:
            assert 0.0 <= result.hurst_exponent <= 1.0

    def test_round_lot_ratio_in_range(self) -> None:
        """round_lot_ratio should be in [0, 1]."""
        volumes = [100.0, 200.0, 150.0, 300.0, 175.0]
        result = trade_size_distribution_report(volumes, round_lot=100.0)
        assert 0.0 <= result.round_lot_ratio <= 1.0

    def test_round_lot_ratio_all_round(self) -> None:
        """All volumes multiples of round_lot -> ratio = 1.0."""
        volumes = [100.0, 200.0, 300.0, 500.0]
        result = trade_size_distribution_report(volumes, round_lot=100.0)
        assert result.round_lot_ratio == 1.0

    def test_round_lot_ratio_none_round(self) -> None:
        """No volumes multiples of round_lot -> ratio = 0.0."""
        volumes = [101.0, 203.0, 307.0, 509.0]
        result = trade_size_distribution_report(volumes, round_lot=100.0)
        assert result.round_lot_ratio == 0.0

    def test_size_autocorr_lag1_in_range(self) -> None:
        """size_autocorr_lag1 should be in [-1, 1]."""
        volumes = [100.0 + i * 10 for i in range(50)]
        result = trade_size_distribution_report(volumes, round_lot=100.0)
        assert -1.0 <= result.size_autocorr_lag1 <= 1.0

    def test_concentration_gini_in_range(self) -> None:
        """concentration_gini should be in [0, 1]."""
        volumes = [100.0 + i * 10 for i in range(50)]
        result = trade_size_distribution_report(volumes, round_lot=100.0)
        assert 0.0 <= result.concentration_gini <= 1.0

    def test_to_dict(self) -> None:
        volumes = [100.0, 200.0, 150.0, 300.0]
        result = trade_size_distribution_report(volumes, round_lot=100.0)
        d = result.to_dict()
        assert "distribution_stats" in d
        assert "pareto_alpha" in d
        assert "hurst_exponent" in d
        assert "round_lot_ratio" in d
        assert "size_autocorr_lag1" in d
        assert "concentration_gini" in d

    def test_empty_volumes_raises(self) -> None:
        with pytest.raises(ValueError):
            trade_size_distribution_report([])

    def test_single_value_volumes(self) -> None:
        """Single value should still produce stats."""
        volumes = [100.0]
        result = trade_size_distribution_report(volumes, round_lot=100.0)
        assert "mean" in result.distribution_stats

    def test_non_finite_values_raise(self) -> None:
        with pytest.raises(ValueError):
            trade_size_distribution_report([float("inf"), 100.0])
