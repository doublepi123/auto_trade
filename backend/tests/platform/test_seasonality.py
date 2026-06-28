"""Tests for P354 seasonality analysis.

Computes mean/std/t-stat for returns grouped by day-of-week and month,
identifying significant seasonal effects (|t-stat| > 2).
"""

from __future__ import annotations

import pytest

from app.platform.seasonality import SeasonalityResult, seasonality_report


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _weekly_seasonal_returns() -> list[float]:
    """Alternating positive (Mon) and negative (Wed) returns with day_of_week."""
    returns: list[float] = []
    # 8 weeks: Mon=+0.02, Tue=+0.002, Wed=-0.015, Thu=+0.003, Fri=+0.001
    for _ in range(8):
        returns.extend([0.02, 0.002, -0.015, 0.003, 0.001])
    return returns


def _weekly_dow() -> list[int]:
    """Day-of-week: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri."""
    dow: list[int] = []
    for _ in range(8):
        dow.extend([0, 1, 2, 3, 4])
    return dow


# ---------------------------------------------------------------------------
# seasonality_report
# ---------------------------------------------------------------------------


class TestSeasonalityReport:
    def test_weekly_returns_produces_day_of_week_effects(self):
        """Weekly seasonal returns should produce day-of-week effect stats."""
        returns = _weekly_seasonal_returns()
        dow = _weekly_dow()
        result = seasonality_report(returns, day_of_week=dow)
        assert isinstance(result, SeasonalityResult)
        assert len(result.day_of_week_effects) > 0
        # Monday (dow=0) has large positive returns → high t-stat
        monday = result.day_of_week_effects[0]
        assert "mean" in monday
        assert "std" in monday
        assert "t_stat" in monday
        assert "n" in monday
        assert monday["mean"] > 0
        assert monday["n"] == 8

    def test_default_day_of_week_and_months(self):
        """Default uses Mon-Fri and Jan-Dec."""
        returns = [0.01 * (i % 7 - 3) for i in range(252)]
        result = seasonality_report(returns)
        assert len(result.day_of_week_effects) == 5  # Mon-Fri
        assert len(result.month_effects) == 12  # Jan-Dec
        # Check keys
        assert set(result.day_of_week_effects.keys()) == {0, 1, 2, 3, 4}
        assert set(result.month_effects.keys()) == set(range(1, 13))

    def test_significant_effects_detected(self):
        """Monday returns should have |t_stat| > 2."""
        returns = _weekly_seasonal_returns()
        dow = _weekly_dow()
        result = seasonality_report(returns, day_of_week=dow)
        # Monday (0.02 consistently) should have high t-stat
        assert len(result.significant_effects) > 0
        # At least one significant effect should reference day_of_week or month
        for eff in result.significant_effects:
            assert "type" in eff
            assert "key" in eff
            assert "t_stat" in eff
            assert abs(eff["t_stat"]) > 2

    def test_custom_months(self):
        """Custom month list must match returns length."""
        returns = [0.01 * (i % 12 - 6) for i in range(120)]
        months = [(i % 3) + 1 for i in range(120)]  # 1,2,3 cycling
        result = seasonality_report(returns, months=months)
        assert set(result.month_effects.keys()).issubset({1, 2, 3})

    def test_empty_returns_raises(self):
        with pytest.raises(ValueError):
            seasonality_report([])

    def test_non_finite_returns_raises(self):
        with pytest.raises(ValueError):
            seasonality_report([0.01, float("inf")])

    def test_mismatched_day_of_week_length_raises(self):
        with pytest.raises(ValueError):
            seasonality_report([0.01, 0.02], day_of_week=[0])

    def test_invalid_day_of_week_value_raises(self):
        with pytest.raises(ValueError):
            seasonality_report([0.01] * 5, day_of_week=[0, 1, 2, 3, 7])

    def test_to_dict(self):
        returns = _weekly_seasonal_returns()
        dow = _weekly_dow()
        result = seasonality_report(returns, day_of_week=dow)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "day_of_week_effects" in d
        assert "month_effects" in d
        assert "significant_effects" in d
