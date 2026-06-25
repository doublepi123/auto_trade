"""Tests for P220 returns calendar analysis."""

from __future__ import annotations

import math
from datetime import date

import pytest

from app.platform.returns_analysis import (
    monthly_returns_table,
    returns_calendar,
    returns_calendar_dict,
)


def test_empty():
    cal = returns_calendar([])
    assert cal.monthly == []
    assert cal.yearly == []
    assert cal.streaks["max_win_streak"] == 0
    assert cal.summary["n_days"] == 0


def test_single_day():
    cal = returns_calendar([0.01])
    assert len(cal.monthly) == 1
    assert abs(cal.monthly[0].gross_return - 0.01) < 1e-12
    assert cal.streaks["max_win_streak"] == 1


def test_monthly_compounding():
    # three returns in the same month
    cal = returns_calendar([0.1, -0.05, 0.2],
                            [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])
    assert abs(cal.monthly[0].gross_return - (1.1 * 0.95 * 1.2 - 1.0)) < 1e-12


def test_month_boundary_split():
    cal = returns_calendar([0.02, 0.03, 0.04],
                            [date(2024, 1, 30), date(2024, 1, 31), date(2024, 2, 1)])
    assert len(cal.monthly) == 2
    assert abs(cal.monthly[0].gross_return - (1.02 * 1.03 - 1.0)) < 1e-12
    assert abs(cal.monthly[1].gross_return - 0.04) < 1e-12


def test_yearly():
    cal = returns_calendar([0.01, 0.02, 0.03],
                            [date(2023, 12, 28), date(2024, 1, 2), date(2024, 1, 3)])
    years = {y.year for y in cal.yearly}
    assert years == {2023, 2024}


def test_weekday_average():
    # four Mondays: 2000-01-03, 10, 17, 24 are all Mondays
    cal = returns_calendar([0.01, 0.02, -0.01, 0.03],
                            [date(2000, 1, 3), date(2000, 1, 10), date(2000, 1, 17), date(2000, 1, 24)])
    monday = cal.weekday[0]
    assert abs(monday["mean_return"] - 0.0125) < 1e-9
    assert abs(monday["win_rate"] - 0.75) < 1e-9
    assert monday["best"] == 0.03
    assert monday["worst"] == -0.01


def test_streaks():
    cal = returns_calendar([0.01, 0.02, 0.03, -0.01, -0.02, 0.04, 0.05, 0.0, 0.01])
    assert cal.streaks["max_win_streak"] == 3
    assert cal.streaks["max_loss_streak"] == 2
    # flat resets streak; final 0.01 starts a new win streak of 1
    assert cal.streaks["current_streak"] == 1
    assert cal.streaks["current_kind"] == "win"


def test_best_worst_day_with_dates():
    cal = returns_calendar([0.01, -0.05, 0.02],
                            [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])
    assert cal.summary["best_day"] == 0.02
    assert cal.summary["worst_day"] == -0.05
    assert cal.summary["best_day_date"] == "2024-01-04"
    assert cal.summary["worst_day_date"] == "2024-01-03"


def test_synthetic_dates_when_none():
    cal = returns_calendar([0.01, 0.02, 0.03])
    # 2000-01-03 is Monday; three consecutive days → Mon/Tue/Wed
    assert cal.weekday[0]["n"] == 1
    assert cal.weekday[1]["n"] == 1


def test_nan_dropped():
    cal = returns_calendar([0.01, float("nan"), 0.02])
    assert cal.summary["n_days"] == 2
    assert cal.summary["n_dropped"] == 1


def test_monthly_table_shape():
    cal = returns_calendar([0.01, 0.02], [date(2024, 1, 2), date(2024, 3, 4)])
    table = monthly_returns_table(cal.monthly)
    assert 2024 in table
    assert table[2024][1] is not None
    assert table[2024][2] is None  # gap month → None


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        returns_calendar([0.01, 0.02], [date(2024, 1, 1)])


def test_dict_view_serializable():
    d = returns_calendar_dict([0.01, -0.02, 0.03])
    assert "monthly" in d and "yearly" in d and "weekday" in d
    assert "streaks" in d and "summary" in d and "monthly_table" in d


def test_total_loss_return():
    # r == -1.0 → running product zeros the month (no log used → no domain error)
    cal = returns_calendar([-1.0, 0.05], [date(2024, 1, 2), date(2024, 1, 3)])
    assert cal.monthly[0].gross_return == -1.0