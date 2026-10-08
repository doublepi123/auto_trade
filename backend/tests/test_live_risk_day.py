"""Unit tests for the US overnight risk-day boundary (20:00 ET)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.core.live_risk_day import risk_day_for
from app.core.market_calendar import trade_day_for

ET = ZoneInfo("America/New_York")
HKT = ZoneInfo("Asia/Hong_Kong")


def _et(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(
        day.year, day.month, day.day, hour, minute, second, tzinfo=ET
    ).astimezone(timezone.utc)


class TestUsBoundary:
    def test_19_59_59_is_same_day(self) -> None:
        assert risk_day_for("US", _et(date(2026, 10, 8), 19, 59, 59)) == date(2026, 10, 8)

    def test_20_00_00_is_next_day(self) -> None:
        assert risk_day_for("US", _et(date(2026, 10, 8), 20, 0, 0)) == date(2026, 10, 9)

    def test_23_59_is_next_day(self) -> None:
        assert risk_day_for("US", _et(date(2026, 10, 8), 23, 59)) == date(2026, 10, 9)

    def test_00_00_is_same_day(self) -> None:
        assert risk_day_for("US", _et(date(2026, 10, 8), 0, 0)) == date(2026, 10, 8)

    def test_03_55_is_same_day(self) -> None:
        assert risk_day_for("US", _et(date(2026, 10, 8), 3, 55)) == date(2026, 10, 8)


class TestDst:
    """EDT (UTC-4) and EST (UTC-5) examples plus the change days themselves."""

    def test_edt_example_2026_10_08_evening_rolls(self) -> None:
        # 2026-10-08 is EDT (UTC-4): 20:30 ET = 00:30 UTC next day.
        assert risk_day_for("US", _et(date(2026, 10, 8), 20, 30)) == date(2026, 10, 9)

    def test_est_example_2026_11_02_evening_rolls(self) -> None:
        # 2026-11-02 is EST (UTC-5): 20:30 ET = 01:30 UTC next day.
        assert risk_day_for("US", _et(date(2026, 11, 2), 20, 30)) == date(2026, 11, 3)

    def test_fall_back_day_2026_11_01_boundary(self) -> None:
        # DST ends 2026-11-01 02:00 EDT -> 01:00 EST. Evening instants are
        # EST; the wall-clock rule is unchanged.
        assert risk_day_for("US", _et(date(2026, 11, 1), 19, 59, 59)) == date(2026, 11, 1)
        assert risk_day_for("US", _et(date(2026, 11, 1), 20, 0)) == date(2026, 11, 2)

    def test_spring_forward_day_2027_03_14_boundary(self) -> None:
        # DST starts 2027-03-14 02:00 EST -> 03:00 EDT. Evening instants are
        # already EDT; the wall-clock rule is unchanged.
        assert risk_day_for("US", _et(date(2027, 3, 14), 19, 59, 59)) == date(2027, 3, 14)
        assert risk_day_for("US", _et(date(2027, 3, 14), 20, 0)) == date(2027, 3, 15)


class TestCalendarEdges:
    def test_year_end_rolls_into_next_year(self) -> None:
        assert risk_day_for("US", _et(date(2026, 12, 31), 20, 0)) == date(2027, 1, 1)

    def test_friday_20_et_maps_to_saturday(self) -> None:
        # 2026-10-09 is a Friday. Weekends are NOT skipped: this is an
        # accounting bucket, not a trading permission.
        assert date(2026, 10, 9).weekday() == 4
        assert risk_day_for("US", _et(date(2026, 10, 9), 20, 0)) == date(2026, 10, 10)

    def test_naive_datetime_is_treated_as_utc(self) -> None:
        # 2026-10-09 00:30 UTC naive == the 20:30 ET instant above.
        naive = datetime(2026, 10, 9, 0, 30)
        assert risk_day_for("US", naive) == date(2026, 10, 9)
        assert risk_day_for("US", naive) == risk_day_for(
            "US", datetime(2026, 10, 9, 0, 30, tzinfo=timezone.utc)
        )

    def test_none_instant_returns_a_date(self) -> None:
        assert isinstance(risk_day_for("US"), date)


class TestOtherMarkets:
    def test_hk_matches_trade_day_for(self) -> None:
        for instant in (
            datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc),  # 10:00 HKT
            datetime(2026, 10, 8, 17, 0, tzinfo=timezone.utc),  # 01:00 HKT next day
            _et(date(2026, 10, 8), 21, 0),  # US overnight window
        ):
            assert risk_day_for("HK", instant) == trade_day_for("HK", instant)

    def test_unknown_market_matches_trade_day_for(self) -> None:
        instant = _et(date(2026, 10, 8), 21, 0)
        assert risk_day_for("ZZ", instant) == trade_day_for("ZZ", instant)
        # ...even though an unknown market resolves to the US session in
        # get_session: only the literal US market gets the overnight rule.
        assert risk_day_for("ZZ", instant) == date(2026, 10, 8)
