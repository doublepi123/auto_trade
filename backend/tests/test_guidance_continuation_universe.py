"""Universe liquidity filters (PREREGISTRATION §10.2)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.domain.guidance_continuation.universe import (
    PriorDayBar,
    evaluate_universe,
    expected_history_days,
)


def _bars(
    n: int = 20,
    close: str = "100",
    volume: int = 1_500_000,
) -> tuple[PriorDayBar, ...]:
    """Twenty real US trading days ending 2026-09-21 (calendar-derived)."""
    days = expected_history_days(date(2026, 9, 22))
    return tuple(
        PriorDayBar(day=d, close=Decimal(close), volume=volume)
        for d in days[:n]
    )


class TestCalendar:
    def test_b4_expected_history_days_are_real_trading_days(self) -> None:
        days = expected_history_days(date(2026, 9, 22))
        assert len(days) == 20
        # T-1 is 2026-09-21, and Labor Day 2026-09-07 is NOT among them.
        assert days[0] == date(2026, 9, 21)
        assert date(2026, 9, 7) not in days
        # strictly descending, no duplicates, all weekdays, no closures.
        from app.core.holiday_calendar import is_market_closed

        assert len(set(days)) == 20
        assert all(d.weekday() < 5 for d in days)
        assert not any(is_market_closed("US", d) for d in days)
        # contiguous: each next day is the previous trading day.
        prev = date(2026, 9, 22)
        for d in days:
            assert d < prev
            cur = d
            nxt = d
            while True:
                nxt -= timedelta(days=1)
                if nxt.weekday() < 5 and not is_market_closed("US", nxt):
                    break
            assert cur == d
            prev = d

    def test_b4_half_day_counts_as_complete_trading_day(self) -> None:
        # L448: 二十个完整交易日 — a half day is a complete trading day
        # for the history window.  Target 2026-11-30 (Mon): the 20 days
        # before it include Black Friday 2026-11-27 (half day).
        days = expected_history_days(date(2026, 11, 30))
        assert date(2026, 11, 27) in days
        assert len(days) == 20

    def test_b4_duplicate_days_fail(self) -> None:
        days = expected_history_days(date(2026, 9, 22))
        dup = (PriorDayBar(day=days[0], close=Decimal("100"), volume=1_500_000),) * 20
        verdict = evaluate_universe(
            bars=dup, has_split_or_unit_change=False, target_day=date(2026, 9, 22)
        )
        assert not verdict.eligible
        assert verdict.reason_code == "MISSING_HISTORY"

    def test_b4_wrong_day_set_fails_even_with_20_bars(self) -> None:
        # 20 valid weekdays that are NOT the expected set (one day swapped
        # for a non-trading day) must fail.
        days = expected_history_days(date(2026, 9, 22))
        swapped = list(days)
        swapped[5] = date(2026, 9, 7)  # Labor Day — market closed
        bars = tuple(
            PriorDayBar(day=d, close=Decimal("100"), volume=1_500_000)
            for d in swapped
        )
        verdict = evaluate_universe(
            bars=tuple(bars), has_split_or_unit_change=False, target_day=date(2026, 9, 22)
        )
        assert not verdict.eligible
        assert verdict.reason_code == "MISSING_HISTORY"

    def test_b4_calendar_gap_fails(self) -> None:
        # Drop one middle day (19 real days + nothing): 19 bars.
        days = expected_history_days(date(2026, 9, 22))
        gapped = (days[0],) + days[2:]  # skip days[1]
        bars = tuple(
            PriorDayBar(day=d, close=Decimal("100"), volume=1_500_000)
            for d in gapped
        )
        verdict = evaluate_universe(
            bars=bars, has_split_or_unit_change=False, target_day=date(2026, 9, 22)
        )
        assert not verdict.eligible
        assert verdict.reason_code == "MISSING_HISTORY"


class TestUniverse:
    def test_20_valid_days_pass(self) -> None:
        verdict = evaluate_universe(
            bars=_bars(), has_split_or_unit_change=False, target_day=date(2026, 9, 22)
        )
        assert verdict.eligible

    def test_nineteen_days_fails(self) -> None:
        verdict = evaluate_universe(
            bars=_bars(n=19), has_split_or_unit_change=False, target_day=date(2026, 9, 22)
        )
        assert not verdict.eligible
        assert verdict.reason_code == "MISSING_HISTORY"

    def test_twenty_one_days_fails(self) -> None:
        # 21 real bars (extend one extra trading day) is not the exact
        # 20-day window.
        days = expected_history_days(date(2026, 9, 22))
        extra = days + (
            _prev_trading_day(days[-1]),
        )
        bars = tuple(
            PriorDayBar(day=d, close=Decimal("100"), volume=1_500_000)
            for d in extra
        )
        verdict = evaluate_universe(
            bars=bars, has_split_or_unit_change=False, target_day=date(2026, 9, 22)
        )
        assert not verdict.eligible
        assert verdict.reason_code == "MISSING_HISTORY"


def _prev_trading_day(day: date) -> date:
    from app.core.holiday_calendar import is_market_closed

    d = day - timedelta(days=1)
    while d.weekday() >= 5 or is_market_closed("US", d):
        d -= timedelta(days=1)
    return d

    def test_tminus1_close_at_20_passes(self) -> None:
        # close 20 needs volume ≥ 5,000,000 to clear the 1e8 ADV floor.
        bars = _bars(close="20", volume=5_000_000)
        verdict = evaluate_universe(bars=bars, has_split_or_unit_change=False, target_day=date(2026, 9, 22))
        assert verdict.eligible

    def test_tminus1_close_at_500_passes(self) -> None:
        bars = _bars(close="500", volume=1_000_000)
        verdict = evaluate_universe(bars=bars, has_split_or_unit_change=False, target_day=date(2026, 9, 22))
        assert verdict.eligible

    def test_tminus1_close_below_20_fails(self) -> None:
        bars = _bars(close="19.99")
        verdict = evaluate_universe(bars=bars, has_split_or_unit_change=False, target_day=date(2026, 9, 22))
        assert not verdict.eligible
        assert verdict.reason_code == "PRICE_BAND"

    def test_tminus1_close_above_500_fails(self) -> None:
        bars = _bars(close="500.01")
        verdict = evaluate_universe(bars=bars, has_split_or_unit_change=False, target_day=date(2026, 9, 22))
        assert not verdict.eligible
        assert verdict.reason_code == "PRICE_BAND"

    def test_mean_turnover_exactly_at_floor_passes(self) -> None:
        # close=100, volume=1_000_000 → turnover exactly 1e8.
        bars = _bars(close="100", volume=1_000_000)
        verdict = evaluate_universe(bars=bars, has_split_or_unit_change=False, target_day=date(2026, 9, 22))
        assert verdict.eligible

    def test_mean_turnover_just_below_floor_fails(self) -> None:
        bars = _bars(close="100", volume=999_999)
        verdict = evaluate_universe(bars=bars, has_split_or_unit_change=False, target_day=date(2026, 9, 22))
        assert not verdict.eligible
        assert verdict.reason_code == "ADV_FLOOR"

    def test_split_day_fails_regardless(self) -> None:
        verdict = evaluate_universe(
            bars=_bars(), has_split_or_unit_change=True, target_day=date(2026, 9, 22)
        )
        assert not verdict.eligible
        assert verdict.reason_code == "CORPORATE_ACTION"

    def test_zero_volume_bar_fails(self) -> None:
        bars = list(_bars())
        replaced = PriorDayBar(
            day=bars[0].day, close=bars[0].close, volume=0
        )
        bars[0] = replaced
        verdict = evaluate_universe(
            bars=tuple(bars), has_split_or_unit_change=False, target_day=date(2026, 9, 22)
        )
        assert not verdict.eligible
        assert verdict.reason_code == "INVALID_BAR"

    def test_non_positive_close_fails(self) -> None:
        bars = list(_bars())
        bars[3] = PriorDayBar(
            day=bars[3].day, close=Decimal("0"), volume=1_500_000
        )
        verdict = evaluate_universe(
            bars=tuple(bars), has_split_or_unit_change=False, target_day=date(2026, 9, 22)
        )
        assert not verdict.eligible
        assert verdict.reason_code == "INVALID_BAR"

    def test_only_tminus1_close_matters_for_band(self) -> None:
        # Earlier days may lie outside the band; only T−1's close is tested.
        bars = list(_bars(close="100"))
        bars[5] = PriorDayBar(
            day=bars[5].day, close=Decimal("1000"), volume=1_000_000
        )
        verdict = evaluate_universe(
            bars=tuple(bars), has_split_or_unit_change=False, target_day=date(2026, 9, 22)
        )
        assert verdict.eligible

    def test_b4_negative_volume_bar_fails(self) -> None:
        # Volume must be a positive int; negative fails even if the mean
        # would be fine.
        bars = list(_bars())
        bars[7] = PriorDayBar(
            day=bars[7].day, close=Decimal("100"), volume=-1
        )
        verdict = evaluate_universe(
            bars=tuple(bars), has_split_or_unit_change=False, target_day=date(2026, 9, 22)
        )
        assert not verdict.eligible
        assert verdict.reason_code == "INVALID_BAR"
