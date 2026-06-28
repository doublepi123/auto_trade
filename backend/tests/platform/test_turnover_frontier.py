"""P338: turnover frontier report tests."""

import math

import pytest

from app.platform.turnover_frontier import (
    TurnoverFrontierResult,
    turnover_frontier_report,
)


class TestTurnoverFrontier:
    def _build_panel(self, n_assets: int = 5, n_periods: int = 500) -> dict[str, list[float]]:
        """Build a synthetic returns panel with positive Sharpe."""
        import random
        rng = random.Random(42)
        panel: dict[str, list[float]] = {}
        for i in range(n_assets):
            # Each asset has a small positive drift
            panel[f"asset_{i}"] = [0.0002 + rng.gauss(0, 0.02) for _ in range(n_periods)]
        return panel

    def test_basic_frontier(self) -> None:
        panel = self._build_panel()
        result = turnover_frontier_report(panel)

        assert isinstance(result, TurnoverFrontierResult)
        assert len(result.frontier) == 6  # default 6 turnover rates
        for entry in result.frontier:
            assert "turnover" in entry
            assert "gross_sharpe" in entry
            assert "net_sharpe" in entry
            assert "cost_drag" in entry

    def test_net_sharpe_decreasing_with_turnover(self) -> None:
        """Higher turnover should reduce net Sharpe (cost drag increases)."""
        panel = self._build_panel()
        result = turnover_frontier_report(panel)

        net_sharpes = [entry["net_sharpe"] for entry in result.frontier]
        # Should be monotonically decreasing
        for i in range(1, len(net_sharpes)):
            assert net_sharpes[i] <= net_sharpes[i - 1]

    def test_custom_turnover_rates(self) -> None:
        panel = self._build_panel()
        result = turnover_frontier_report(panel, turnover_rates=[0.01, 0.5])

        assert len(result.frontier) == 2

    def test_breakeven_and_optimal(self) -> None:
        panel = self._build_panel()
        result = turnover_frontier_report(panel)

        # breakeven_turnover should be >= 0
        assert result.breakeven_turnover >= 0
        # optimal_turnover should be >= 0
        assert result.optimal_turnover >= 0

    def test_zero_cost(self) -> None:
        """With zero cost, net_sharpe should equal gross_sharpe."""
        panel = self._build_panel()
        result = turnover_frontier_report(panel, cost_per_turnover=0.0)

        for entry in result.frontier:
            assert math.isclose(entry["net_sharpe"], entry["gross_sharpe"], rel_tol=1e-9)

    def test_invalid_empty_panel(self) -> None:
        with pytest.raises(ValueError):
            turnover_frontier_report({})

    def test_invalid_empty_turnover_rates(self) -> None:
        panel = self._build_panel(n_periods=100)
        with pytest.raises(ValueError):
            turnover_frontier_report(panel, turnover_rates=[])

    def test_invalid_non_finite(self) -> None:
        with pytest.raises(ValueError):
            turnover_frontier_report({"a": [float("inf"), 0.01]})

    def test_invalid_negative_turnover(self) -> None:
        panel = self._build_panel(n_periods=100)
        with pytest.raises(ValueError):
            turnover_frontier_report(panel, turnover_rates=[-0.01])

    def test_to_dict(self) -> None:
        panel = self._build_panel(n_periods=100)
        result = turnover_frontier_report(panel, turnover_rates=[0.01, 0.05])
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "frontier" in d
        assert "breakeven_turnover" in d
        assert "optimal_turnover" in d

    def test_single_asset_panel(self) -> None:
        panel = self._build_panel(n_assets=1)
        result = turnover_frontier_report(panel, turnover_rates=[0.01, 0.5])
        assert len(result.frontier) == 2

    def test_unequal_length_panel(self) -> None:
        with pytest.raises(ValueError):
            turnover_frontier_report({"a": [0.01, 0.02], "b": [0.01]})

    def test_insufficient_data_for_sharpe(self) -> None:
        with pytest.raises(ValueError):
            turnover_frontier_report({"a": [0.01]})


def test_turnover_frontier_rejects_negative_cost():
    from app.platform.turnover_frontier import turnover_frontier_report
    with pytest.raises(ValueError):
        turnover_frontier_report({"a": [0.01, 0.02, 0.03]}, cost_per_turnover=-0.01)
