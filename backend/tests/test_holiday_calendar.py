from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core.holiday_calendar import (
    closure_label,
    is_half_day,
    is_market_closed,
    list_closures,
)
from app.core.market_calendar import (
    is_trading_hours,
    market_for_symbol,
    next_session_open,
    trade_day_for,
)


def test_market_closed_on_known_us_holiday():
    # Independence Day 2024 — NYSE closed
    assert is_market_closed("US", date(2024, 7, 4)) is True
    assert closure_label("US", date(2024, 7, 4)) == "Independence Day"


def test_market_open_on_normal_weekday():
    assert is_market_closed("US", date(2024, 7, 2)) is False
    assert is_market_closed("US", date(2024, 7, 5)) is False


def test_hk_lunar_new_year_three_days():
    for d in (date(2025, 1, 29), date(2025, 1, 30), date(2025, 1, 31)):
        assert is_market_closed("HK", d) is True, f"HK should be closed on {d}"


def test_rth_false_on_us_holiday_during_market_hours():
    # 2024-07-04 is a Thursday, 14:30 UTC = 10:30 ET (inside RTH),
    # but the exchange is closed for Independence Day.
    ts = datetime(2024, 7, 4, 14, 30, tzinfo=timezone.utc)
    assert is_trading_hours("US", ts) is False


def test_rth_true_on_normal_weekday():
    ts = datetime(2024, 7, 2, 14, 30, tzinfo=timezone.utc)  # Tuesday 10:30 ET
    assert is_trading_hours("US", ts) is True


def test_half_day_flags_christmas_eve_hk():
    assert is_half_day("HK", date(2024, 12, 24)) is True
    assert is_half_day("HK", date(2024, 7, 2)) is False


def test_nyse_2026_and_2027_early_closes_match_official_calendar():
    # NYSE/ICE publishes only these 2026/2027 cash-equity early closes.
    assert is_half_day("US", date(2026, 11, 27)) is True
    assert is_half_day("US", date(2026, 12, 24)) is True
    assert is_half_day("US", date(2027, 11, 26)) is True

    # 2026-07-02 is a regular cash-equity session. The 14:00 early close
    # announced for that date applies to the bond market, not NYSE equities.
    assert is_half_day("US", date(2026, 7, 2)) is False


def test_list_closures_filters_by_market_and_year():
    items = list_closures("US", 2025)
    assert all(item["market"] == "US" for item in items)
    assert all(item["date"].startswith("2025") for item in items)
    assert any(item["date"] == "2025-12-25" for item in items)


def test_list_closures_returns_empty_for_unknown_market():
    assert list_closures("ZZ", 2025) == []


def test_market_for_symbol_handles_hk_suffix():
    assert market_for_symbol("700.HK") == "HK"
    assert market_for_symbol("AAPL.US") == "US"
    assert market_for_symbol("") == "US"


def test_trade_day_for_resolves_local_clock():
    # 2024-07-04 in HK = Thursday 16:00 HKT == 08:00 UTC.
    # Local trade day should be 2024-07-04 (HK calendar day).
    ts = datetime(2024, 7, 4, 8, 0, tzinfo=timezone.utc)
    assert trade_day_for("HK", ts).isoformat() == "2024-07-04"


def test_next_session_open_skips_holidays():
    # 2024-07-03 is a US half-day (early close), 2024-07-04 is closed.
    # From 14:00 UTC on 2024-07-04, next session open should be 2024-07-05.
    ts = datetime(2024, 7, 4, 14, 0, tzinfo=timezone.utc)
    nxt = next_session_open("US", ts)
    # 09:30 ET == 13:30 UTC (DST)
    assert nxt.hour == 13
    assert nxt.minute == 30
    assert nxt.date().isoformat() == "2024-07-05"


def test_2027_holidays_loaded():
    """The complete official NYSE 2027 closure set backs the API bound."""
    from app.core.holiday_calendar import COVERAGE_END_YEAR, list_closures

    assert COVERAGE_END_YEAR == 2027
    us_2027 = list_closures("US", 2027)
    assert {item["date"] for item in us_2027} == {
        "2027-01-01",
        "2027-01-18",
        "2027-02-15",
        "2027-03-26",
        "2027-05-31",
        "2027-06-18",
        "2027-07-05",
        "2027-09-06",
        "2027-11-25",
        "2027-12-24",
    }
    hk_2027 = list_closures("HK", 2027)
    assert len(hk_2027) >= 10, f"expected at least 10 HKEX closures in 2027, got {len(hk_2027)}"


def test_calendar_closures_api_rejects_out_of_range_year():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    # 2030 is now rejected (data only goes through 2027).
    resp = client.get("/api/calendar/closures", params={"market": "US", "year": 2030})
    assert resp.status_code == 422
    # 2020 is below the coverage start.
    resp = client.get("/api/calendar/closures", params={"market": "US", "year": 2020})
    assert resp.status_code == 422
    # 2027 is in range.
    resp = client.get("/api/calendar/closures", params={"market": "US", "year": 2027})
    assert resp.status_code == 200
    assert resp.json()["coverage_end_year"] == 2027


# ---------------------------------------------------------------------------
# Fail-open guardrails. is_market_closed means "known full-day closure" only.
# Treating unknown dates as closed would make is_rth() return False
# (market_calendar.py:51-52), which disables EOD flatten (runner.py:6576 via
# is_closing_window) and RTH_ONLY stop-loss exits
# (trade_execution_service.py:866-874). These must stay GREEN forever.
# ---------------------------------------------------------------------------


def test_is_market_closed_fail_open_for_uncovered_2030_weekday():
    # WHY: pinning fail-open by name. A 2030 weekday is outside the static
    # table; is_market_closed must return False ("not a known closure"), not
    # True. Making unknown dates "closed" would disable EOD flatten
    # (runner.py:6576) and RTH_ONLY exits (trade_execution_service.py:866-874).
    assert is_market_closed("US", date(2030, 1, 2)) is False


def test_is_market_closed_fail_open_for_pre_coverage_2023_weekday():
    # WHY: dates before COVERAGE_START_YEAR are also fail-open. test_rotation_forward.py:42
    # builds _sessions(date(2023, 1, 3), ...) against the real is_market_closed
    # and relies on this. Making unknown dates "closed" would disable EOD flatten
    # (runner.py:6576) and RTH_ONLY exits (trade_execution_service.py:866-874).
    assert is_market_closed("US", date(2023, 1, 3)) is False


@pytest.mark.parametrize(
    "day",
    [
        date(2028, 1, 17),
        date(2028, 2, 21),
        date(2028, 4, 14),
        date(2028, 5, 29),
        date(2028, 6, 19),
        date(2028, 7, 4),
        date(2028, 9, 4),
        date(2028, 11, 23),
        date(2028, 12, 25),
    ],
)
def test_us_2028_full_day_closures(day: date):
    assert is_market_closed("US", day) is True


def test_us_2028_new_years_is_not_a_closure():
    # 2028-01-01 falls on a Saturday; NYSE observes no New Year's holiday in 2028.
    assert is_market_closed("US", date(2028, 1, 1)) is False


def test_us_2027_nye_is_not_a_closure():
    assert is_market_closed("US", date(2027, 12, 31)) is False


def test_us_2028_independence_eve_and_black_friday_are_half_days():
    assert is_half_day("US", date(2028, 7, 3)) is True
    assert is_half_day("US", date(2028, 11, 24)) is True


def test_us_2028_christmas_eve_is_sunday_so_not_a_half_day():
    # 2028-12-24 is a Sunday; there is no Christmas Eve early close.
    assert is_half_day("US", date(2028, 12, 24)) is False


@pytest.mark.parametrize(
    "day",
    [
        date(2027, 2, 5),
        date(2027, 12, 24),
        date(2027, 12, 31),
    ],
)
def test_hk_2027_half_day_sessions(day: date):
    # Official HKEX circular ce_SEHK_CT_077_2026.pdf: these sessions close 12:00.
    # 2027-12-24 is currently also listed as a full-day closure; that conflict
    # is deliberately left untested here (no is_market_closed assertion).
    assert is_half_day("HK", day) is True


def test_coverage_end_years_is_per_market():
    from app.core.holiday_calendar import COVERAGE_END_YEARS

    assert COVERAGE_END_YEARS == {"US": 2028, "HK": 2027}


@pytest.mark.parametrize(
    ("market", "day", "expired"),
    [
        ("US", date(2029, 1, 2), True),
        ("US", date(2028, 6, 1), False),
        ("HK", date(2028, 1, 3), True),
        ("HK", date(2027, 6, 1), False),
        ("US", date(2023, 5, 2), False),
        ("XX", date(2026, 1, 5), True),
    ],
)
def test_is_coverage_expired(market: str, day: date, expired: bool):
    from app.core.holiday_calendar import is_coverage_expired

    assert is_coverage_expired(market, day) is expired


def test_coverage_horizon_stays_at_least_a_year_ahead():
    """Rule-enforcing: fail CI ~12 months before the calendar can mislead live trading.

    Past the horizon `pre_submit_risk_check` refuses new entries, so an expired
    calendar halts entry rather than trading blind. This test makes that a
    build-time failure instead of a silent runtime cliff.
    """
    from app.core.holiday_calendar import COVERAGE_END_YEARS

    today = date.today()
    for market, end_year in sorted(COVERAGE_END_YEARS.items()):
        assert date(end_year, 12, 31) >= today.replace(year=today.year + 1), (
            f"{market} holiday coverage ends {end_year}-12-31, under a year away. "
            f"Extend _FULL_DAY_CLOSURES and _HALF_DAY_SESSIONS in "
            f"app/core/holiday_calendar.py from the official "
            f"{'NYSE' if market == 'US' else 'HKEX'} calendar, then raise "
            f"COVERAGE_END_YEARS['{market}']."
        )
