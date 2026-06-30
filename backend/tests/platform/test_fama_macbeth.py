"""P371 fama_macbeth tests — TDD RED phase."""

from __future__ import annotations

import math


class TestFamaMacbeth:
    """Test fama_macbeth_report."""

    def test_positive_premium_with_correlated_factor(self):
        """Factor returns positively correlated with asset returns → mean_premium > 0."""
        from app.platform.fama_macbeth import fama_macbeth_report

        # Create a panel where factor is positively related to returns
        returns_panel = {
            "A": [0.01, 0.02, -0.005, 0.015, 0.01, -0.008, 0.012, 0.005, 0.018, -0.002],
            "B": [0.015, 0.025, -0.002, 0.02, 0.012, -0.005, 0.015, 0.008, 0.022, 0.0],
            "C": [0.02, 0.03, 0.0, 0.025, 0.015, -0.003, 0.018, 0.01, 0.025, 0.003],
        }
        factor_panel = {
            "F1": [0.01, 0.015, -0.003, 0.012, 0.008, -0.005, 0.01, 0.005, 0.015, -0.001],
        }

        result = fama_macbeth_report(returns_panel, factor_panel)

        assert result.summary["mean_premium"] > 0.0
        assert math.isfinite(result.summary["t_stat"])
        assert math.isfinite(result.summary["std_premium"])
        assert isinstance(result.summary["significant"], bool)
        assert len(result.per_period_premiums) == 10

    def test_per_period_structure(self):
        """Each per_period entry has period, premium, r_squared."""
        from app.platform.fama_macbeth import fama_macbeth_report

        returns_panel = {
            "A": [0.01, 0.02, -0.005],
            "B": [0.015, 0.025, -0.002],
        }
        factor_panel = {
            "F1": [0.01, 0.015, -0.003],
        }

        result = fama_macbeth_report(returns_panel, factor_panel)

        assert len(result.per_period_premiums) == 3
        for item in result.per_period_premiums:
            assert isinstance(item["period"], int)
            assert math.isfinite(item["premium"])
            assert math.isfinite(item["r_squared"])
            assert 0.0 <= item["r_squared"] <= 1.0

    def test_t_stat_existence(self):
        """t_stat is computed from mean and std."""
        from app.platform.fama_macbeth import fama_macbeth_report

        returns_panel = {
            "A": [0.01, 0.02, -0.005, 0.015, 0.01],
            "B": [0.015, 0.025, -0.002, 0.02, 0.012],
            "C": [0.02, 0.03, 0.0, 0.025, 0.015],
        }
        factor_panel = {
            "F1": [0.01, 0.015, -0.003, 0.012, 0.008],
        }

        result = fama_macbeth_report(returns_panel, factor_panel)
        summary = result.summary

        assert abs(summary["mean_premium"] - summary["t_stat"] * summary["std_premium"] / math.sqrt(5)) < 0.01

    def test_mismatched_periods_raise(self):
        """Factors and returns must have equal periods."""
        import pytest
        from app.platform.fama_macbeth import fama_macbeth_report

        returns_panel = {"A": [0.01, 0.02]}
        factor_panel = {"F1": [0.01, 0.02, 0.03]}

        with pytest.raises(ValueError):
            fama_macbeth_report(returns_panel, factor_panel)

    def test_non_finite_returns_raise(self):
        """NaN/inf in panel raises ValueError."""
        import pytest
        from app.platform.fama_macbeth import fama_macbeth_report

        returns_panel = {"A": [float('nan'), 0.01]}
        factor_panel = {"F1": [0.01, 0.02]}

        with pytest.raises(ValueError):
            fama_macbeth_report(returns_panel, factor_panel)

    def test_max_assets_limited(self):
        """Panel with > 50 assets raises ValueError."""
        import pytest
        from app.platform.fama_macbeth import fama_macbeth_report

        returns_panel = {f"A{i}": [0.01, 0.02] for i in range(51)}
        factor_panel = {"F1": [0.01, 0.02]}

        with pytest.raises(ValueError):
            fama_macbeth_report(returns_panel, factor_panel)

    def test_to_dict(self):
        """Result is JSON-serialisable via to_dict()."""
        from app.platform.fama_macbeth import fama_macbeth_report

        returns_panel = {"A": [0.01, 0.02, -0.005]}
        factor_panel = {"F1": [0.01, 0.015, -0.003]}

        result = fama_macbeth_report(returns_panel, factor_panel)
        d = result.to_dict()
        assert d["per_period_premiums"] == result.per_period_premiums
        assert d["summary"] == result.summary
