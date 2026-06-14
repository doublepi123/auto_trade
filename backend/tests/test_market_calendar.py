from __future__ import annotations

from datetime import date, datetime, timezone

from freezegun import freeze_time

from zoneinfo import ZoneInfo

from app.core.market_calendar import (
    get_session,
    is_trading_hours,
    next_session_open,
    trade_day_for,
)


class TestTradeDay:
    def test_us_morning_session_is_same_day(self) -> None:
        # 2026-05-22 14:00 UTC = 2026-05-22 10:00 ET (RTH)
        instant = datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc)
        assert trade_day_for("US", instant) == date(2026, 5, 22)

    def test_us_after_hours_before_utc_midnight_stays_on_session_day(self) -> None:
        # 2026-05-22 23:00 UTC = 2026-05-22 19:00 ET (after-hours, same calendar day)
        instant = datetime(2026, 5, 22, 23, 0, tzinfo=timezone.utc)
        assert trade_day_for("US", instant) == date(2026, 5, 22)

    def test_us_early_utc_morning_belongs_to_previous_local_day(self) -> None:
        # 2026-05-23 01:00 UTC = 2026-05-22 21:00 ET (still that session's late evening)
        instant = datetime(2026, 5, 23, 1, 0, tzinfo=timezone.utc)
        assert trade_day_for("US", instant) == date(2026, 5, 22)

    def test_hk_morning_uses_local_date(self) -> None:
        # 2026-05-22 02:00 UTC = 2026-05-22 10:00 HKT (RTH)
        instant = datetime(2026, 5, 22, 2, 0, tzinfo=timezone.utc)
        assert trade_day_for("HK", instant) == date(2026, 5, 22)

    def test_hk_evening_utc_already_past_midnight_local(self) -> None:
        # 2026-05-22 17:00 UTC = 2026-05-23 01:00 HKT (next local day)
        instant = datetime(2026, 5, 22, 17, 0, tzinfo=timezone.utc)
        assert trade_day_for("HK", instant) == date(2026, 5, 23)

    def test_unknown_market_defaults_to_us(self) -> None:
        instant = datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc)
        assert trade_day_for("ZZ", instant) == trade_day_for("US", instant)


class TestIsTradingHours:
    def test_us_during_rth(self) -> None:
        # 2026-05-22 14:30 UTC = 09:30 ET
        assert is_trading_hours("US", datetime(2026, 5, 22, 14, 30, tzinfo=timezone.utc))

    def test_us_pre_market(self) -> None:
        # 2026-05-22 13:00 UTC = 08:00 ET (pre-market)
        assert not is_trading_hours("US", datetime(2026, 5, 22, 13, 0, tzinfo=timezone.utc))

    def test_weekend_is_closed(self) -> None:
        # 2026-05-23 = Saturday
        assert not is_trading_hours("US", datetime(2026, 5, 23, 14, 30, tzinfo=timezone.utc))
        assert not is_trading_hours("HK", datetime(2026, 5, 23, 2, 0, tzinfo=timezone.utc))

    def test_hk_during_rth(self) -> None:
        # 2026-05-22 02:00 UTC = 10:00 HKT
        assert is_trading_hours("HK", datetime(2026, 5, 22, 2, 0, tzinfo=timezone.utc))

    def test_hk_next_session_open_after_lunch_is_same_day_resume(self) -> None:
        # 2026-05-22 04:30 UTC = 12:30 HKT (lunch break)
        instant = datetime(2026, 5, 22, 4, 30, tzinfo=timezone.utc)
        resume = next_session_open("HK", instant)
        assert resume.astimezone(ZoneInfo("Asia/Hong_Kong")).hour == 13
        assert resume.astimezone(ZoneInfo("Asia/Hong_Kong")).minute == 0

    def test_hk_lunch_break_is_not_rth(self) -> None:
        # 2026-05-22 04:30 UTC = 12:30 HKT (lunch break)
        assert not is_trading_hours("HK", datetime(2026, 5, 22, 4, 30, tzinfo=timezone.utc))


class TestNextSessionOpen:
    def test_next_open_skips_weekend(self) -> None:
        # 2026-05-22 is a Friday; Mon 2026-05-25 is Memorial Day (US holiday);
        # so the next open is Tuesday 2026-05-26 at 09:30 ET = 13:30 UTC.
        friday_close = datetime(2026, 5, 22, 21, 0, tzinfo=timezone.utc)  # ~17:00 ET Fri
        next_open = next_session_open("US", friday_close)
        assert next_open.weekday() == 1  # Tuesday
        assert next_open.hour == 13 and next_open.minute == 30
        assert next_open.date().isoformat() == "2026-05-26"

    def test_next_open_skips_weekend_plain(self) -> None:
        # Use a holiday-free weekend for a pure "skip Sat/Sun" assertion.
        # 2024-12-13 is a Friday with no adjacent holidays.
        friday_close = datetime(2024, 12, 13, 21, 0, tzinfo=timezone.utc)
        next_open = next_session_open("US", friday_close)
        assert next_open.weekday() == 0  # Monday
        assert next_open.date().isoformat() == "2024-12-16"

    def test_next_open_within_session_advances_to_next_day(self) -> None:
        rth_instant = datetime(2026, 5, 22, 14, 30, tzinfo=timezone.utc)
        next_open = next_session_open("US", rth_instant)
        # Next open is the following business day 09:30 ET
        assert next_open.date() > rth_instant.date()


def test_session_metadata() -> None:
    us = get_session("US")
    assert us.code == "US"
    assert us.rth_open.hour == 9 and us.rth_open.minute == 30
    assert us.rth_close.hour == 16


class TestDstBoundary:
    """Cover US DST boundaries and simultaneous US/HK local trade days."""

    def test_us_dst_spring_forward_march_8_2026_keeps_trade_day(self) -> None:
        with freeze_time("2026-03-08 06:30:00", tz_offset=0):
            day = trade_day_for("US", datetime.now(timezone.utc))

        assert day == date(2026, 3, 8)

    def test_us_dst_fall_back_nov_1_2026_keeps_trade_day(self) -> None:
        with freeze_time("2026-11-01 05:30:00", tz_offset=0):
            day = trade_day_for("US", datetime.now(timezone.utc))

        assert day == date(2026, 11, 1)

    def test_us_rth_utc_open_moves_after_dst_boundaries(self) -> None:
        with freeze_time("2026-03-09 13:30:00", tz_offset=0):
            assert is_trading_hours("US", datetime.now(timezone.utc)) is True
        with freeze_time("2026-11-02 14:30:00", tz_offset=0):
            assert is_trading_hours("US", datetime.now(timezone.utc)) is True

    def test_hk_dst_no_change_across_us_spring_boundary(self) -> None:
        with freeze_time("2026-03-08 16:00:00", tz_offset=0):
            day = trade_day_for("HK", datetime.now(timezone.utc))

        assert day == date(2026, 3, 9)

    def test_us_hk_simultaneous_trade_day_can_differ(self) -> None:
        with freeze_time("2026-06-04 16:00:00", tz_offset=0):
            us_day = trade_day_for("US", datetime.now(timezone.utc))
            hk_day = trade_day_for("HK", datetime.now(timezone.utc))

        assert us_day == date(2026, 6, 4)
        assert hk_day == date(2026, 6, 5)
