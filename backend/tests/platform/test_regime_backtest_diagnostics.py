"""P324 regime_backtest_diagnostics tests — TDD RED phase."""

from __future__ import annotations

import math

import pytest

from app.platform.regime_backtest_diagnostics import regime_backtest_diagnostics_report


class TestRegimeBacktestDiagnostics:
    """Test regime_backtest_diagnostics_report."""

    def test_bull_sharpe_gt_bear(self):
        """Bull regime (positive returns) → Sharpe > bear regime Sharpe."""
        from app.platform.regime_backtest_diagnostics import (
            regime_backtest_diagnostics_report,
        )

        returns = [
            0.01, 0.02, -0.03, 0.015, 0.01, -0.02, 0.02, 0.01, -0.01, 0.005,
            0.015, 0.01, -0.005, 0.02, -0.01, 0.01, 0.02, -0.03, 0.01, 0.015,
            0.01, 0.02, -0.02, 0.015, 0.01, -0.025, 0.018, 0.012, -0.01, 0.008,
        ]
        regimes = [
            "bull", "bull", "bear", "bull", "bull", "bear", "bull", "bull", "bear", "bull",
            "bull", "bull", "bear", "bull", "bear", "bull", "bull", "bear", "bull", "bull",
            "bull", "bull", "bear", "bull", "bull", "bear", "bull", "bull", "bear", "bull",
        ]
        result = regime_backtest_diagnostics_report(returns, regimes)

        diagnostics = result.diagnostics
        assert "bull" in diagnostics
        assert "bear" in diagnostics
        bull_s = diagnostics["bull"]["sharpe"]
        bear_s = diagnostics["bear"]["sharpe"]
        assert bull_s > bear_s

    def test_per_regime_stats(self):
        """Each regime entry has sharpe, win_rate, mean, std."""
        from app.platform.regime_backtest_diagnostics import (
            regime_backtest_diagnostics_report,
        )

        returns = [0.01, -0.01, 0.02, -0.02, 0.01, -0.005]
        regimes = ["up", "down", "up", "down", "up", "down"]
        result = regime_backtest_diagnostics_report(returns, regimes)

        for regime_name, stats in result.diagnostics.items():
            assert "sharpe" in stats
            assert "win_rate" in stats
            assert "mean" in stats
            assert "std" in stats
            assert 0.0 <= stats["win_rate"] <= 1.0

    def test_with_trade_outcomes(self):
        """With trade_outcomes, per-regime trade count and avg_pnl are included."""
        from app.platform.regime_backtest_diagnostics import (
            regime_backtest_diagnostics_report,
        )

        returns = [0.01, 0.02, -0.01, 0.005, -0.005, 0.015]
        regimes = ["bull", "bull", "bear", "bull", "bear", "bull"]
        trade_outcomes = [(0, 0.5), (1, -0.2), (2, 0.3), (3, -0.1), (5, 0.4)]
        result = regime_backtest_diagnostics_report(returns, regimes, trade_outcomes)

        for stats in result.diagnostics.values():
            assert "trade_count" in stats
            assert "avg_pnl" in stats or stats["trade_count"] == 0

    def test_invalid_inputs_raise(self):
        """Invalid inputs raise ValueError."""
        import pytest
        from app.platform.regime_backtest_diagnostics import (
            regime_backtest_diagnostics_report,
        )

        with pytest.raises(ValueError):
            regime_backtest_diagnostics_report([], [])

        with pytest.raises(ValueError):
            regime_backtest_diagnostics_report([1.0, 2.0], ["A"])

        with pytest.raises(ValueError):
            regime_backtest_diagnostics_report([float('nan'), 2.0], ["A", "B"])

    def test_to_dict(self):
        """Result is JSON-serialisable via to_dict()."""
        from app.platform.regime_backtest_diagnostics import (
            regime_backtest_diagnostics_report,
        )

        returns = [0.01, -0.01, 0.02, -0.02]
        regimes = ["up", "down", "up", "down"]
        result = regime_backtest_diagnostics_report(returns, regimes)
        d = result.to_dict()
        assert d["diagnostics"] == result.diagnostics
        for regime_name, stats in result.diagnostics.items():
            assert isinstance(stats.get("sharpe"), float)


def test_regime_backtest_rejects_trade_outcomes_without_indices():
    with pytest.raises(ValueError):
        regime_backtest_diagnostics_report([0.01, 0.02], ["bull", "bear"], trade_outcomes=[0.01])
