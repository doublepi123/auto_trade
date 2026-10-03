"""Phase-aware close windows for AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED.

With the extended-hours trading flag effective, the entry cutoff (>=45 min)
and flatten (>=15 min) P0 minimums are measured from the end of the LAST
executable phase of the trading day: 20:00 ET normally, the RTH close on days
without post-market (half days, non-US markets). Flag off keeps the existing
RTH-close semantics untouched.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.execution_session import (
    extended_last_executable_close,
    is_extended_closing_window,
)
from app.core.market_calendar import is_closing_window

_ET = ZoneInfo("America/New_York")
_HK = ZoneInfo("Asia/Hong_Kong")


def _et(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=_ET)


class TestExtendedLastExecutableClose:
    def test_normal_us_day_ends_at_2000_et(self) -> None:
        # 2026-10-19 is a Monday.
        close = extended_last_executable_close("US", _et("2026-10-19T12:00:00"))
        assert close is not None
        assert close == datetime(2026, 10, 19, 20, 0, tzinfo=_ET)

    def test_half_day_post_unavailable_ends_at_rth_close(self) -> None:
        # 2026-11-27 Black Friday early close 13:00 ET; post-market unsupported.
        close = extended_last_executable_close("US", _et("2026-11-27T10:00:00"))
        assert close is not None
        assert close == datetime(2026, 11, 27, 13, 0, tzinfo=_ET)

    def test_hk_has_no_extended_phase_so_ends_at_rth_close(self) -> None:
        close = extended_last_executable_close(
            "HK",
            datetime(2026, 10, 12, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")),
        )
        assert close is not None
        assert close == datetime(2026, 10, 12, 16, 0, tzinfo=_HK)

    def test_weekend_returns_none(self) -> None:
        # 2026-10-24 is a Saturday.
        assert extended_last_executable_close("US", _et("2026-10-24T12:00:00")) is None

    def test_holiday_returns_none(self) -> None:
        assert extended_last_executable_close("US", _et("2026-12-25T12:00:00")) is None

    def test_instant_after_rth_same_day_still_2000_et(self) -> None:
        # 17:30 ET Monday: the last executable phase of TODAY still ends 20:00.
        close = extended_last_executable_close("US", _et("2026-10-19T17:30:00"))
        assert close is not None
        assert close == datetime(2026, 10, 19, 20, 0, tzinfo=_ET)

    def test_coverage_expired_returns_none(self) -> None:
        # Far future: the static calendar cannot classify the day.
        assert (
            extended_last_executable_close("US", _et("2031-01-06T12:00:00")) is None
        )


class TestLegacyClosingWindowUnchanged:
    """Flag-off semantics must be unchanged: windows measured from RTH close."""

    def test_1520_inside_45min_cutoff_window(self) -> None:
        # 45 minutes before 16:00 is 15:15; 15:20 is inside the window.
        assert is_closing_window("US", 45, _et("2026-10-19T15:20:00")) is True

    def test_1550_inside_15min_flatten_window(self) -> None:
        assert is_closing_window("US", 15, _et("2026-10-19T15:50:00")) is True

    def test_post_market_outside_legacy_closing_window(self) -> None:
        # Outside RTH the legacy predicate is False — unchanged even at 19:50.
        assert is_closing_window("US", 15, _et("2026-10-19T19:50:00")) is False


class TestExtendedClosingWindow:
    """Flag-effective windows measured from the last executable phase end."""

    def test_cutoff_boundary_at_1915_et(self) -> None:
        from app.core.execution_session import is_extended_closing_window

        # 45 minutes before 20:00 ET is 19:15.
        assert is_extended_closing_window("US", 45, _et("2026-10-19T19:20:00")) is True
        assert is_extended_closing_window("US", 45, _et("2026-10-19T19:10:00")) is False

    def test_flatten_boundary_at_1945_et(self) -> None:
        from app.core.execution_session import is_extended_closing_window

        # 15 minutes before 20:00 ET is 19:45.
        assert is_extended_closing_window("US", 15, _et("2026-10-19T19:50:00")) is True
        assert is_extended_closing_window("US", 15, _et("2026-10-19T19:40:00")) is False

    def test_no_window_at_1520_or_1550_et(self) -> None:
        from app.core.execution_session import is_extended_closing_window

        assert is_extended_closing_window("US", 45, _et("2026-10-19T15:20:00")) is False
        assert is_extended_closing_window("US", 15, _et("2026-10-19T15:50:00")) is False

    def test_half_day_boundaries_relative_to_1300_et(self) -> None:
        from app.core.execution_session import is_extended_closing_window

        # Black Friday: cutoff 12:15, flatten 12:45, both before 13:00.
        assert is_extended_closing_window("US", 45, _et("2026-11-27T12:20:00")) is True
        assert is_extended_closing_window("US", 45, _et("2026-11-27T12:10:00")) is False
        assert is_extended_closing_window("US", 15, _et("2026-11-27T12:50:00")) is True
        # Post 13:00 nothing is executable, so no window later in the day.
        assert is_extended_closing_window("US", 15, _et("2026-11-27T14:00:00")) is False

    def test_weekend_and_holiday_have_no_window(self) -> None:
        from app.core.execution_session import is_extended_closing_window

        assert is_extended_closing_window("US", 15, _et("2026-10-24T19:50:00")) is False
        assert is_extended_closing_window("US", 15, _et("2026-12-25T19:50:00")) is False
